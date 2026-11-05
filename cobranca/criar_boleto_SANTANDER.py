import json
import re
import unicodedata
from datetime import date, datetime

import requests

# PDF do boleto. Import protegido: se o boleto_pdf_santander.py ou o
# reportlab faltarem, o boleto é emitido normalmente, só sem PDF,
# e o processador (inclusive BB/ABC) continua funcionando.
try:
    from boleto_pdf_santander import gerar_pdf_boleto
except Exception as _erro_import_pdf:
    gerar_pdf_boleto = None
    print(
        "AVISO SANTANDER: geração de PDF indisponível "
        f"({_erro_import_pdf}). Instale o reportlab e copie o boleto_pdf_santander.py."
    )

# ============================================================
# CARREGAR PARÂMETROS
# ============================================================

PARAMETROS = "parametros_prod.txt"


def carregar_parametros(arquivo):
    parametros = {}

    with open(arquivo, "r", encoding="utf-8-sig") as f:
        for linha in f:
            linha = linha.strip().lstrip("\ufeff")

            if not linha or linha.startswith("#"):
                continue

            if "=" in linha:
                chave, valor = linha.split("=", 1)
                parametros[chave.strip()] = valor.strip().strip('"').strip("'")

    return parametros


parametros = carregar_parametros(PARAMETROS)

nsu_contador = int(parametros.get("SANTANDER_NSU", "1"))

# PDFs de boletos gerados nesta execução do processador. O processador
# lê esta lista no fim e envia todos num zip para o Slack.
PDFS_GERADOS_NESTA_EXECUCAO = []
ren_contador = int(parametros.get("SANTANDER_REN", "1"))


# ============================================================
# SALVAR CONTADORES NO parametros_prod.txt
# ============================================================

def salvar_contador(arquivo, chave, valor):
    with open(arquivo, "r", encoding="utf-8") as f:
        linhas = f.readlines()

    nova_linha = f"{chave}={valor}\n"
    encontrado = False
    novas_linhas = []

    for linha in linhas:
        if linha.strip().startswith(f"{chave}="):
            novas_linhas.append(nova_linha)
            encontrado = True
        else:
            novas_linhas.append(linha)

    if not encontrado:
        if novas_linhas and not novas_linhas[-1].endswith("\n"):
            novas_linhas[-1] += "\n"
        novas_linhas.append(nova_linha)

    with open(arquivo, "w", encoding="utf-8") as f:
        f.writelines(novas_linhas)


# ============================================================
# CONFIGURAÇÃO SANTANDER POR HOUSE BANK
#
# O convênio informado pelo Santander pertence à empresa A
# (dados do convênio no arquivo de parâmetros).
# ============================================================

CONFIGURACAO_HOUSE_BANK = {

    "SAN01": {
        "covenantCode": parametros["SANTANDER_Convenio_PROD01"],
        "workspaceId": parametros["SANTANDER_WorkspaceId_PROD01"],
        "ClientID": parametros["SANTANDER_ClientID_PROD01"],
        "ClientSecret": parametros["SANTANDER_ClientSecret_PROD01"],
        "ChavePix": parametros["SANTANDER_ChavePix_PROD01"],
        "TipoChavePix": "EVP",
        "CompanyCodes": ["BR01"],

        # Dados impressos no PDF do boleto
        "Agencia": parametros.get("SANTANDER_Agencia_PROD01", "0000"),
        "BeneficiarioNome": parametros.get(
            "SANTANDER_BeneficiarioNome_PROD01",
            "EMPRESA A INDUSTRIA E COMERCIO LTDA",
        ),
        "BeneficiarioEndereco": parametros.get(
            "SANTANDER_BeneficiarioEndereco_PROD01",
            "RUA EXEMPLO, 100 - CENTRO - CIDADE - UF - CEP: 00000-000",
        ),

        # CNPJ do titular do convênio (beneficiário original). Nota 7: o
        # beneficiário final só é enviado se for de OUTRA raiz de CNPJ.
        "CNPJTitular": parametros.get("SANTANDER_CNPJTitular_PROD01", "11222333000181"),

        # Nota 21/22: código receptor de partilha (2 dígitos) cadastrado
        # pelo banco no convênio. Vazio = sem partilha.
        "CodigoPartilha": parametros.get("SANTANDER_CodigoPartilha_PROD01", ""),
    },
}

# Pasta local onde o PDF é salvo (o processador copia para a
# PASTA_PDF_COMPARTILHADA com nome amigável)
SANTANDER_PASTA_PDF = parametros.get("SANTANDER_PASTA_PDF", "boletos_santander")


# ============================================================
# AMBIENTE / URLS
# ============================================================

SANTANDER_AMBIENTE = parametros.get("SANTANDER_AMBIENTE_PROD", "PRODUCAO").upper()

if SANTANDER_AMBIENTE not in ("TESTE", "PRODUCAO"):
    raise ValueError(
        f"SANTANDER_AMBIENTE_PROD inválido: '{SANTANDER_AMBIENTE}'. "
        f"A API só aceita TESTE ou PRODUCAO."
    )

SANTANDER_URL = parametros["SANTANDER_URL_PROD"].rstrip("/")

# Ambiente REAL da API, pela URL. O processador usa isso para não
# gravar no SAP nem no histórico quando o boleto é de sandbox.
SANTANDER_AMBIENTE_API = "SANDBOX" if "sandbox" in SANTANDER_URL.lower() else "PRODUCAO"
SANTANDER_TOKEN_URL = parametros["SANTANDER_TOKEN_URL_PROD"]


# ============================================================
# CERTIFICADO e-CNPJ A1 (mTLS)
# ============================================================

_cert_path = parametros.get("SANTANDER_CERT_PATH")
_key_path = parametros.get("SANTANDER_KEY_PATH")

SANTANDER_CERT = (_cert_path, _key_path) if _cert_path and _key_path else None

if SANTANDER_CERT is None:
    print(
        "AVISO SANTANDER: SANTANDER_CERT_PATH/SANTANDER_KEY_PATH não configurados. "
        "Sem o certificado e-CNPJ A1 a API tende a recusar com HTTP 403."
    )


# ============================================================
# BENEFICIÁRIO FINAL (mesmo de-para do BB)
# ============================================================

DEPARA_COMPANY_CODE = {
    "BR01": {"Nome": "EMPRESA A LTDA", "CNPJ": "11222333000181"},
}


# ============================================================
# REGRAS DE COBRANÇA — espelhando o criar_boleto_BB.py
# (None = campo não é enviado)
# ============================================================

DOCUMENT_KIND = "DUPLICATA_MERCANTIL"   # BB: codigoTipoTitulo 2
PAYMENT_TYPE = "REGISTRO"               # BB: recebimento parcial "N"

INTEREST_PERCENTAGE = 2.5               # BB: JUROS_TIPO 2 / 2,5% a.m.

FINE_PERCENTAGE = None                  # BB: MULTA_TIPO 0 (sem multa)
FINE_QUANTITY_DAYS = None

DEDUCTION_VALUE = None                  # BB: VALOR_ABATIMENTO 0

PROTEST_TYPE = "DIAS_CORRIDOS"          # BB: QUANTIDADE_DIAS_PROTESTO 9
PROTEST_QUANTITY_DAYS = 9

WRITE_OFF_QUANTITY_DAYS = 90            # BB: NUMERO_DIAS_LIMITE_RECEBIMENTO 90

DISCOUNT_TYPE = None                    # BB: desconto tipo 0 (sem desconto)

BANK_NUMBER_DIGITOS = 10                # mesmos 10 dígitos finais do nosso número BB
CLIENT_NUMBER_MAX = 15

# Nota 9: com instrução de protesto o seu número deve ter até 11 posições
CLIENT_NUMBER_MAX_PROTESTO = 11
PARTICIPANT_CODE_MAX = 25

UFS_VALIDAS = {
    "AC", "AL", "AM", "AP", "BA", "CE", "DF", "ES", "GO", "MA", "MG", "MS",
    "MT", "PA", "PB", "PE", "PI", "PR", "RJ", "RN", "RO", "RR", "RS", "SC",
    "SE", "SP", "TO",
}


# ============================================================
# RETENTATIVA POR NOSSO NÚMERO DUPLICADO (igual ao BB)
#
# O Santander não documenta um código fixo para "nosso número
# já existente", então a detecção é feita pelo campo/mensagem
# do erro. Quando acontecer pela primeira vez, confira o
# _errorCode impresso no log e inclua em CODIGOS_ERRO_DUPLICADO.
# ============================================================

ITEM_MAXIMO = 999
CODIGOS_ERRO_DUPLICADO = set()
PALAVRAS_DUPLICIDADE = ("DUPLIC", "JA EXISTE", "JÁ EXISTE", "EXISTENTE", "CADASTRAD")


def eh_erro_nosso_numero_duplicado(resposta_json):
    if not isinstance(resposta_json, dict):
        return False

    if str(resposta_json.get("_errorCode", "")) in CODIGOS_ERRO_DUPLICADO:
        return True

    for erro in resposta_json.get("_errors") or []:
        if not isinstance(erro, dict):
            continue

        if str(erro.get("_code", "")) in CODIGOS_ERRO_DUPLICADO:
            return True

        campo = str(erro.get("_field", "")).lower()
        mensagem = str(erro.get("_message", "")).upper()

        if "banknumber" in campo and any(p in mensagem for p in PALAVRAS_DUPLICIDADE):
            return True

    return False


def somar_item(item):
    return str(int(item) + 1).zfill(3)


# ============================================================
# FUNÇÕES AUXILIARES DE FORMATAÇÃO
# ============================================================

def _texto(valor, tamanho):
    texto = (
        unicodedata.normalize("NFKD", str(valor if valor is not None else ""))
        .encode("ASCII", "ignore")
        .decode("ASCII")
        .upper()
    )
    texto = re.sub(r"\s+", " ", texto).strip()
    return texto[:tamanho]


def _texto_banco(valor, tamanho, permitir_e_comercial=False, padrao=""):
    """
    Regra de validação do Santander para nomes e endereço:
    somente letras, números e espaço (no nome também '&').
    Pontuação vira espaço: 'LTDA.' -> 'LTDA', 'JD. AMERICA' -> 'JD AMERICA',
    'N° 12-A' -> 'N 12 A'. Acentos já são removidos pelo _texto.
    """
    permitidos = r"[^A-Z0-9 &]" if permitir_e_comercial else r"[^A-Z0-9 ]"
    texto = re.sub(permitidos, " ", _texto(valor, 500))
    texto = re.sub(r"\s+", " ", texto).strip()[:tamanho].strip()
    return texto or padrao


def nome_arquivo_boleto(cliente, referencia, item):
    """
    Nome do PDF no padrão dos boletos do Santander:
        AGRICOLA_AUTO_PECAS_LTDA_NF_36956-1.pdf
    cliente   : razão social (acentos e pontuação removidos, espaços viram _)
    referencia: DocumentReferenceID_2 (ex.: 001825121-1 -> NF 36956)
    item      : item contábil = parcela (ex.: 001 -> 1)
    """
    nome = re.sub(r"[^A-Z0-9]+", "_", _texto(cliente, 200)).strip("_")[:60].rstrip("_") or "CLIENTE"

    numero = str(referencia).split("-", 1)[0].strip()
    if numero.isdigit():
        nf = str(int(numero))
    else:
        nf = re.sub(r"[^A-Z0-9]+", "_", _texto(referencia, 40)).strip("_") or "SEM_REF"

    try:
        parcela = str(int(item))
    except (TypeError, ValueError):
        parcela = re.sub(r"\D", "", str(item)) or "1"

    return f"{nome}_NF_{nf}-{parcela}.pdf"


def _alfanumerico(valor, tamanho, manter_final=False):
    """Somente letras, números e espaço (regra de clientNumber/participantCode)."""
    texto = re.sub(r"[^0-9A-Za-z ]", "", _texto(valor, 200)).strip()
    return texto[-tamanho:] if manter_final else texto[:tamanho]


def _valor(valor):
    return f"{float(valor):.2f}"


def _cep(cep):
    digitos = re.sub(r"\D", "", str(cep))
    if len(digitos) != 8:
        raise ValueError(f"CEP inválido: {cep}")
    return f"{digitos[:5]}-{digitos[5:]}"


def _data_iso(valor):
    """Converte dd.mm.aaaa (padrão SAP/BB) para AAAA-MM-DD (padrão Santander)."""
    texto = str(valor).strip()

    for formato in ("%d.%m.%Y", "%Y-%m-%d", "%d/%m/%Y", "%Y%m%d"):
        try:
            return datetime.strptime(texto, formato).date().isoformat()
        except ValueError:
            continue

    raise ValueError(f"Data em formato não reconhecido: {valor}")


def _documento(numero, tipo):
    digitos = re.sub(r"[^0-9A-Za-z]", "", str(numero)).upper()
    return digitos.zfill(14 if tipo == "CNPJ" else 11)


# ============================================================
# NÚMEROS DO TÍTULO — mesma regra do gerar_numeros_titulo do BB
# ============================================================

def gerar_numeros_titulo(document_reference, item):
    """
    Retorna (client_number, bank_number, usa_item, usa_ren).

    1. REN. EM ANEXO -> RENEG + contador (5 dígitos)
    2. ADT           -> referência completa + item
    3. Demais        -> referência até o 1º hífen + item
    """
    referencia = document_reference.upper()

    if referencia.startswith("REN. EM ANEXO") or referencia.startswith("REN.EM ANEXO"):
        titulo_beneficiario = "RENEG" + str(ren_contador).zfill(5)
        usa_item = False
        usa_ren = True

    elif referencia.startswith("ADT"):
        titulo_beneficiario = document_reference.strip() + item
        usa_item = True
        usa_ren = False

    else:
        titulo_beneficiario = document_reference.split("-", 1)[0].strip() + item
        usa_item = True
        usa_ren = False

    # Seu número: Santander só aceita letras/números (hífen do ADT é removido)
    limite = (
        CLIENT_NUMBER_MAX_PROTESTO
        if PROTEST_TYPE in ("DIAS_CORRIDOS", "DIAS_UTEIS", "CADASTRO_CONVENIO")
        else CLIENT_NUMBER_MAX
    )
    client_number = _alfanumerico(titulo_beneficiario, limite, manter_final=True)

    # Nosso número: mesmos 10 dígitos finais usados no BB (letras viram 0)
    numerico = re.sub(r"\D", "0", titulo_beneficiario)
    bank_number = numerico[-BANK_NUMBER_DIGITOS:].zfill(BANK_NUMBER_DIGITOS)

    return client_number, bank_number, usa_item, usa_ren


# ============================================================
# NSU
#
# nsuCode deve ser único por convênio/data. Avança a cada envio
# que chegou na API (com sucesso ou erro), para nunca repetir.
# Em ambiente TESTE o Santander espera o prefixo "TST".
# ============================================================

def proximo_nsu_code():
    global nsu_contador

    numero = nsu_contador
    nsu_contador += 1
    salvar_contador(PARAMETROS, "SANTANDER_NSU", nsu_contador)

    if SANTANDER_AMBIENTE == "TESTE":
        return "TST" + str(numero).zfill(17)

    return str(numero)


# ============================================================
# TOKEN
# ============================================================

def obter_token(configuracao):
    print()
    print("=" * 70)
    print("AUTENTICAÇÃO SANTANDER")
    print("=" * 70)

    resposta = requests.post(
        SANTANDER_TOKEN_URL,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        data={
            "client_id": configuracao["ClientID"],
            "client_secret": configuracao["ClientSecret"],
            "grant_type": "client_credentials",
        },
        cert=SANTANDER_CERT,
        timeout=30,
    )

    print(f"HTTP STATUS TOKEN: {resposta.status_code}")

    try:
        dados_token = resposta.json()
    except ValueError:
        print()
        print("RESPOSTA NÃO JSON:")
        print(resposta.text)

        dica = ""
        if resposta.status_code == 403:
            dica = (
                " (403 na borda do Santander: normalmente certificado e-CNPJ "
                "ausente/inválido ou credencial não vinculada a ele)"
            )

        raise ValueError(
            "API Santander não retornou uma resposta JSON ao solicitar o token" + dica
        )

    if resposta.status_code != 200:
        print()
        print("ERRO AO OBTER TOKEN:")
        print(json.dumps(dados_token, indent=4, ensure_ascii=False))
        resposta.raise_for_status()

    token = dados_token.get("access_token")

    if not token:
        raise ValueError("API Santander não retornou access_token.")

    print("Access token obtido com sucesso.")

    return token


# ============================================================
# NORMALIZAR RESPOSTA PARA O FORMATO LIDO PELO PROCESSADOR
# ============================================================

def normalizar_resposta(resposta_json, payload):
    if not isinstance(resposta_json, dict):
        return resposta_json

    # Sucesso: processador lê "numero"
    if "digitableLine" in resposta_json or "barcode" in resposta_json or "barCode" in resposta_json:
        resposta_json.setdefault("numero", resposta_json.get("bankNumber", payload["bankNumber"]))
        resposta_json.setdefault("linhaDigitavel", resposta_json.get("digitableLine"))
        resposta_json.setdefault("codigoBarraNumerico", resposta_json.get("barCode") or resposta_json.get("barcode"))

    # Erro: processador lê erros[0].codigo / mensagem / ocorrencia
    elif "_errorCode" in resposta_json or "_errors" in resposta_json:
        erros = []

        for erro in resposta_json.get("_errors") or []:
            if isinstance(erro, dict):
                erros.append({
                    "codigo": erro.get("_code") or resposta_json.get("_errorCode"),
                    "mensagem": erro.get("_message") or resposta_json.get("_message"),
                    "ocorrencia": erro.get("_field"),
                })

        if not erros:
            erros.append({
                "codigo": resposta_json.get("_errorCode"),
                "mensagem": " - ".join(
                    filter(None, [resposta_json.get("_message"), resposta_json.get("_details")])
                ),
                "ocorrencia": resposta_json.get("_traceId"),
            })

        resposta_json.setdefault("erros", erros)

    return resposta_json


# ============================================================
# FUNÇÃO DE CRIAÇÃO
# ============================================================

def criacaoSANTANDER(linha):

    global ren_contador

    # ========================================================
    # HOUSE BANK
    # ========================================================

    house_bank = str(linha["HouseBank"]).strip()
    configuracao = CONFIGURACAO_HOUSE_BANK.get(house_bank)

    if configuracao is None:
        raise ValueError(f"HouseBank não parametrizado: {house_bank}")

    covenant_code = re.sub(r"\D", "", str(configuracao["covenantCode"]))
    workspace_id = configuracao["workspaceId"]

    # ========================================================
    # CAMPOS SAP
    # ========================================================

    company_code = str(linha["CompanyCode"]).strip()
    accounting_document = str(linha["AccountingDocument"]).strip()
    accounting_document_item = str(linha["AccountingDocumentItem"]).strip()
    fiscal_year = str(linha["FiscalYear"]).strip()
    document_reference = str(linha["DocumentReferenceID_2"]).strip()
    customer = str(linha["Customer"]).strip()
    bp_tax_type = str(linha["BPTaxType"]).strip()
    bp_tax_number = str(linha["BPTaxNumber"]).strip()

    if company_code not in configuracao.get("CompanyCodes", [company_code]):
        print(
            f"AVISO: CompanyCode {company_code} sendo emitido no convênio "
            f"Santander de {configuracao.get('CompanyCodes')} ({house_bank})."
        )

    data_emissao = _data_iso(linha["Dt_lcto"])
    data_vencimento = _data_iso(linha["NetDueDate"])
    valor_original = float(linha["AmountInCompanyCodeCurrency"])

    # ========================================================
    # PAGADOR
    # ========================================================

    pagador_tipo = {"BR1": "CNPJ", "BR2": "CPF"}.get(bp_tax_type)

    if pagador_tipo is None:
        raise ValueError(f"BPTaxType inválido: {bp_tax_type}")

    pagador_uf = str(linha["Region"]).strip().upper()

    if pagador_uf not in UFS_VALIDAS:
        raise ValueError(f"UF do pagador inválida para o Santander: {pagador_uf}")

    pagador = {
        "name": _texto_banco(linha["BusinessPartnerFullName"], 40, permitir_e_comercial=True),
        "documentType": pagador_tipo,
        "documentNumber": _documento(bp_tax_number, pagador_tipo),
        "address": _texto_banco(f"{linha['StreetName']} {linha['HouseNumber']}", 40, padrao="NAO INFORMADO"),
        "neighborhood": _texto_banco(linha["District"], 30, padrao="NAO INFORMADO"),
        "city": _texto_banco(linha["CityName"], 20),
        "state": pagador_uf,
        "zipCode": _cep(linha["PostalCode"]),
    }

    # ========================================================
    # BENEFICIÁRIO FINAL
    # ========================================================

    dados_beneficiario = DEPARA_COMPANY_CODE.get(company_code)

    if dados_beneficiario is None:
        raise ValueError(
            f"CompanyCode não parametrizado no DEPARA_COMPANY_CODE: {company_code}"
        )

    # ========================================================
    # NÚMEROS DO TÍTULO
    # ========================================================

    item_atual = accounting_document_item

    client_number, bank_number, usa_item, usa_ren = gerar_numeros_titulo(
        document_reference, item_atual
    )

    # Equivalente ao campoUtilizacaoBeneficiario do BB (21 posições)
    participant_code = _alfanumerico(
        customer + accounting_document + fiscal_year, PARTICIPANT_CODE_MAX
    )

    # ========================================================
    # PAYLOAD
    # ========================================================

    payload = {
        "environment": SANTANDER_AMBIENTE,
        "nsuCode": None,                       # preenchido a cada tentativa
        "nsuDate": date.today().isoformat(),
        "covenantCode": covenant_code,
        "bankNumber": bank_number,
        "clientNumber": client_number,
        "dueDate": data_vencimento,
        "issueDate": data_emissao,
        "participantCode": participant_code,
        "nominalValue": _valor(valor_original),
        "payer": pagador,
        "documentKind": DOCUMENT_KIND,
        "paymentType": PAYMENT_TYPE,
    }

    if INTEREST_PERCENTAGE is not None:
        payload["interestPercentage"] = _valor(INTEREST_PERCENTAGE)

    if FINE_PERCENTAGE is not None:
        payload["finePercentage"] = _valor(FINE_PERCENTAGE)
        if FINE_QUANTITY_DAYS is not None:
            payload["fineQuantityDays"] = str(FINE_QUANTITY_DAYS)

    if DEDUCTION_VALUE is not None:
        payload["deductionValue"] = _valor(DEDUCTION_VALUE)

    if PROTEST_TYPE is not None:
        payload["protestType"] = PROTEST_TYPE
        if PROTEST_TYPE in ("DIAS_CORRIDOS", "DIAS_UTEIS"):
            payload["protestQuantityDays"] = str(PROTEST_QUANTITY_DAYS)

    if WRITE_OFF_QUANTITY_DAYS is not None:
        payload["writeOffQuantityDays"] = str(WRITE_OFF_QUANTITY_DAYS)

    if DISCOUNT_TYPE is not None:
        payload["discount"] = {"type": DISCOUNT_TYPE}

    chave_pix = str(configuracao.get("ChavePix") or "").strip()

    # Nota 7: beneficiário final só quando o recurso é de terceiro
    # (raiz de CNPJ diferente do titular do convênio)
    raiz_beneficiario = _documento(dados_beneficiario["CNPJ"], "CNPJ")[:8]
    raiz_titular = _documento(configuracao["CNPJTitular"], "CNPJ")[:8]

    if raiz_beneficiario != raiz_titular:
        payload["beneficiary"] = {
            "name": _texto_banco(dados_beneficiario["Nome"], 40, permitir_e_comercial=True),
            "documentType": "CNPJ",
            "documentNumber": _documento(dados_beneficiario["CNPJ"], "CNPJ"),
        }

    # Nota 21/22: partilha de 100% para a conta do código receptor
    codigo_partilha = re.sub(r"\D", "", str(configuracao.get("CodigoPartilha") or ""))

    if codigo_partilha:
        payload["sharing"] = [{
            "code": codigo_partilha.zfill(2)[-2:],
            "value": payload["nominalValue"],
        }]

    if chave_pix:
        payload["key"] = {
            "type": configuracao.get("TipoChavePix", "CNPJ"),
            "dictKey": chave_pix,
        }

    # ========================================================
    # DEBUG
    # ========================================================

    print()
    print("=" * 70)
    print("BOLETO SANTANDER GERADO")
    print("=" * 70)
    print(f"HouseBank: {house_bank}")
    print(f"Convênio: {covenant_code}")
    print(f"Workspace: {workspace_id}")
    print(f"Cliente: {customer}")
    print(f"Documento: {accounting_document}")
    print(f"Item: {accounting_document_item}")
    print(f"Valor: {valor_original}")
    print(f"Vencimento: {data_vencimento}")
    print(f"Seu número (clientNumber): {client_number}")
    print(f"Nosso número (bankNumber): {bank_number}")

    # ========================================================
    # TOKEN / HEADERS
    # ========================================================

    token = obter_token(configuracao)

    headers = {
        "Authorization": f"Bearer {token}",
        "X-Application-Key": configuracao["ClientID"],
        "Accept": "application/json",
        "Content-Type": "application/json",
    }

    endpoint = (
        f"{SANTANDER_URL}/collection_bill_management/v2/"
        f"workspaces/{workspace_id}/bank_slips"
    )

    print()
    print("=" * 70)
    print("ENVIANDO BOLETO PARA API SANTANDER")
    print("=" * 70)
    print(f"Endpoint: {endpoint}")

    # ========================================================
    # ENVIO COM RETENTATIVA (igual ao BB)
    # ========================================================

    tentativa = 1

    while True:

        payload["nsuCode"] = proximo_nsu_code()

        print()
        print(
            f"Tentativa {tentativa} | Item: {item_atual} | "
            f"Nosso número: {payload['bankNumber']} | NSU: {payload['nsuCode']}"
        )
        print(json.dumps(payload, indent=4, ensure_ascii=False))

        resposta = requests.post(
            endpoint,
            headers=headers,
            json=payload,
            cert=SANTANDER_CERT,
            timeout=60,
        )

        try:
            resposta_json = resposta.json()
        except ValueError:
            resposta_json = None

        if resposta.status_code in (200, 201):
            break

        if not eh_erro_nosso_numero_duplicado(resposta_json):
            break

        print(f"Nosso número {payload['bankNumber']} já existe no Santander (item {item_atual}).")

        if not usa_item:
            break

        if int(item_atual) >= ITEM_MAXIMO:
            print(f"Item chegou em {ITEM_MAXIMO}, não é possível somar mais.")
            break

        # Só o nosso número muda; o seu número continua com o item original
        item_atual = somar_item(item_atual)
        _, payload["bankNumber"], _, _ = gerar_numeros_titulo(document_reference, item_atual)
        tentativa += 1

    # ========================================================
    # REN — só avança se o boleto foi aceito
    # ========================================================

    if usa_ren and resposta.status_code in (200, 201):
        ren_contador += 1
        salvar_contador(PARAMETROS, "SANTANDER_REN", ren_contador)
        print(f"SANTANDER_REN atualizado: {ren_contador}")

    # ========================================================
    # RESPOSTA
    # ========================================================

    print()
    print("=" * 70)
    print("RESPOSTA API SANTANDER")
    print("=" * 70)
    print(f"HTTP STATUS: {resposta.status_code}")
    print()

    if resposta_json is None:
        print(resposta.text)
    else:
        print(json.dumps(resposta_json, indent=4, ensure_ascii=False))

    sucesso = resposta.status_code in (200, 201)

    # ========================================================
    # PDF DO BOLETO
    #
    # Falha no PDF NÃO desfaz o registro: o boleto já existe no
    # banco. Nesse caso pdf_arquivo volta None e o aviso fica no log.
    # ========================================================

    pdf_arquivo = None

    if sucesso and isinstance(resposta_json, dict) and gerar_pdf_boleto is not None:
        try:
            pdf_arquivo, pdf_teste = gerar_pdf_boleto(
                payload,
                resposta_json,
                {
                    "nome": configuracao["BeneficiarioNome"],
                    "cnpj": dados_beneficiario["CNPJ"],
                    "endereco": configuracao["BeneficiarioEndereco"],
                    "agencia": configuracao["Agencia"],
                    "pix": bool(payload.get("key")),
                },
                SANTANDER_PASTA_PDF,
                nome_arquivo_boleto(
                    linha["BusinessPartnerFullName"], document_reference, accounting_document_item
                ),
            )
            print()
            print(f"PDF do boleto gerado: {pdf_arquivo}")
            if not pdf_teste:
                PDFS_GERADOS_NESTA_EXECUCAO.append(pdf_arquivo)
            if pdf_teste:
                print(
                    "AVISO: PDF marcado como DOCUMENTO DE TESTE - NÃO PAGAR "
                    "(ambiente TESTE ou valor do código de barras diferente do título)."
                )
        except Exception as erro:
            print()
            print(f"AVISO: boleto registrado, mas o PDF não foi gerado: {erro}")
    erro_referencia = (not sucesso) and eh_erro_nosso_numero_duplicado(resposta_json)

    resposta_json = normalizar_resposta(resposta_json, payload)
    dados = resposta_json if isinstance(resposta_json, dict) else {}

    # O Santander só devolve 201 no sucesso; 200 é tratado igual
    status_code = 201 if sucesso else resposta.status_code

    return {
        "status_code": status_code,
        "resposta": resposta_json,
        "payload": payload,
        "item_utilizado": item_atual,
        "tentativas": tentativa,

        # ---- campos lidos pelo processador (avaliar_resposta) ----
        "ambiente": SANTANDER_AMBIENTE_API,
        "nosso_numero": dados.get("bankNumber") or payload["bankNumber"],
        "linha_digitavel": dados.get("digitableLine"),
        "codigo_barras": dados.get("barCode") or dados.get("barcode"),
        "data_registro": dados.get("entryDate"),
        "nome_pagador": payload["payer"]["name"],
        "cnpj_pagador": payload["payer"]["documentNumber"],
        # PIX só conta se foi pedido (chave enviada) e o banco devolveu o QR Code.
        # O sandbox devolve um QR Code fixo mesmo sem pedir.
        "pix": bool(payload.get("key")) and bool(dados.get("qrCodePix")),
        "qr_code_pix": dados.get("qrCodePix"),
        "erro_referencia": erro_referencia,
        "pdf_arquivo": pdf_arquivo,
    }


# ============================================================
# README — CONVERTER O CERTIFICADO .pfx PARA .pem
#
# O requests não lê .pfx. Com o OpenSSL instalado (vem junto
# com o Git for Windows), rodar uma vez:
#
#   openssl pkcs12 -in empresa.pfx -clcerts -nokeys -out empresa_cert.pem
#   openssl pkcs12 -in empresa.pfx -nocerts -nodes  -out empresa_key.pem
#
# (se der erro de algoritmo no OpenSSL 3, acrescentar -legacy)
#
# O empresa_key.pem fica SEM senha: guardar com acesso restrito.
# ============================================================