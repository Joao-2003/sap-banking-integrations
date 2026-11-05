import json
import requests
import re
import unicodedata

# ============================================================
# CARREGAR PARÂMETROS
# ============================================================

PARAMETROS = "parametros_prod.txt"


def carregar_parametros(arquivo):
    parametros = {}

    with open(arquivo, "r", encoding="utf-8") as f:
        for linha in f:
            linha = linha.strip()

            if not linha or linha.startswith("#"):
                continue

            if "=" in linha:
                chave, valor = linha.split("=", 1)

                parametros[chave.strip()] = (
                    valor.strip()
                    .strip('"')
                    .strip("'")
                )

    return parametros


parametros = carregar_parametros(PARAMETROS)

ren_contador = int(parametros.get("REN", "1"))

# ============================================================
# SALVAR PRÓXIMO REN NO parametros_prod.txt
# ============================================================

def salvar_ren(arquivo, proximo_ren):

    with open(
        arquivo,
        "r",
        encoding="utf-8"
    ) as f:

        linhas = f.readlines()


    nova_linha_ren = f"REN={proximo_ren}\n"

    ren_encontrado = False

    novas_linhas = []

    for linha in linhas:

        if linha.strip().startswith("REN="):

            novas_linhas.append(
                nova_linha_ren
            )

            ren_encontrado = True

        else:

            novas_linhas.append(
                linha
            )


    # Caso REN ainda não exista no arquivo
    if not ren_encontrado:

        novas_linhas.append(
            nova_linha_ren
        )


    with open(
        arquivo,
        "w",
        encoding="utf-8"
    ) as f:

        f.writelines(
            novas_linhas
        )

# ============================================================
# CONFIGURAÇÃO BANCO DO BRASIL POR HOUSE BANK
# ============================================================

CONFIGURACAO_HOUSE_BANK = {

    # Empresa C
     "BB002": {
         "numeroConvenio": 1234567,
         "numeroCarteira": 17,
         "numeroVariacaoCarteira": 19,
         "codigoModalidade": 1,
         "ClientID" : parametros["BB_ClientID_PROD02"],
         "ClientSecret" : parametros["BB_ClientSecret_PROD02"],
         "appKey" : parametros["BB_appKey_PROD02"]
     },

    # Empresa A
    "BB003": {
        "numeroConvenio": 1234567,
        "numeroCarteira": 17,
        "numeroVariacaoCarteira": 19,
        "codigoModalidade": 1,
        "ClientID" : parametros["BB_ClientID_PROD03"],
        "ClientSecret" : parametros["BB_ClientSecret_PROD03"],
        "appKey" : parametros["BB_appKey_PROD03"]
    },

    # Empresa B
     "BB004": {
         "numeroConvenio": 1234567,
         "numeroCarteira": 17,
         "numeroVariacaoCarteira": 19,
         "codigoModalidade": 1,
         "ClientID" : parametros["BB_ClientID_PROD04"],
         "ClientSecret" : parametros["BB_ClientSecret_PROD04"],
         "appKey" : parametros["BB_appKey_PROD04"]
     },

}


# ============================================================
# BANCO DO BRASIL
# ============================================================

BB_AMBIENTE = parametros["BB_AMBIENTE_PROD"]
BB_URL = parametros["BB_URL_PROD"]
BB_TOKEN_URL = parametros["BB_TOKEN_URL_PROD"]
BB_ENDPOINT_REGISTRO = (
    f"{BB_URL}/boletos"
)

# ============================================================
# OBTER ACCESS TOKEN BB
# ============================================================

def obter_token(configuracao):

    print()
    print("=" * 70)
    print("AUTENTICAÇÃO BANCO DO BRASIL")
    print("=" * 70)

    BB_ClientID = configuracao["ClientID"]

    BB_ClientSecret = configuracao["ClientSecret"]
    
    resposta = requests.post(

        BB_TOKEN_URL,

        auth=(
            BB_ClientID,
            BB_ClientSecret
        ),

        headers={
            "Content-Type":
                "application/x-www-form-urlencoded",

            "Accept":
                "application/json"
        },

        data={
            "grant_type":
                "client_credentials",

            "scope":
                "cobrancas.boletos-requisicao"
        },

        timeout=30
    )

    print(
        f"HTTP STATUS TOKEN: {resposta.status_code}"
    )

    try:

        dados_token = resposta.json()

    except ValueError:

        print()
        print("RESPOSTA NÃO JSON:")
        print(resposta.text)

        raise ValueError(
            "API BB não retornou uma resposta JSON ao solicitar o token."
        )

    if resposta.status_code != 200:

        print()
        print("ERRO AO OBTER TOKEN:")

        print(
            json.dumps(
                dados_token,
                indent=4,
                ensure_ascii=False
            )
        )

        resposta.raise_for_status()

    token = dados_token.get(
        "access_token"
    )

    if not token:

        raise ValueError(
            "API BB não retornou access_token."
        )

    print()
    print("Access token obtido com sucesso.")

    print(
        "Token:",
        token[:10] + "..." + token[-5:]
    )

    return token

# ============================================================
# DE-PARA COMPANY CODE -> DADOS BB
# ============================================================

DEPARA_COMPANY_CODE = {
    "BR01": {
        "Nome": "EMPRESA A LTDA",
        "CNPJ": "11222333000181",
        "BB": "BB003"
    },
    "BR02": {
        "Nome": "EMPRESA B LTDA",
        "CNPJ": "44555666000110",
        "BB": "BB004"
    },
    "BR04": {
        "Nome": "EMPRESA C LTDA",
        "CNPJ": "77888999000155",
        "BB": "BB002"
    }
}



# ============================================================
# CONFIGURAÇÕES FIXAS - BANCO DO BRASIL
# ============================================================

VALOR_ABATIMENTO = 0
QUANTIDADE_DIAS_PROTESTO = 9
QUANTIDADE_DIAS_NEGATIVACAO = 0
ORGAO_NEGATIVADOR = 0

INDICADOR_ACEITE_TITULO_VENCIDO = "S"
NUMERO_DIAS_LIMITE_RECEBIMENTO = 90
CODIGO_ACEITE = "N"

CODIGO_TIPO_TITULO = 2
DESCRICAO_TIPO_TITULO = ""

INDICADOR_PERMISSAO_RECEBIMENTO_PARCIAL = "N"


# ============================================================
# JUROS
# ============================================================

JUROS_TIPO = 2
JUROS_PORCENTAGEM = 2.5
JUROS_VALOR = 0


# ============================================================
# MULTA
# ============================================================

MULTA_TIPO = 0
MULTA_DATA = None
MULTA_PORCENTAGEM = 0
MULTA_VALOR = 0


# ============================================================
# PIX
# ============================================================

INDICADOR_PIX = "S"
ID_LOCATION_PIX = 0
ID_LOCATION_RECORRENCIA = 0


# ============================================================
# BENEFICIÁRIO FINAL
# ============================================================

BENEFICIARIO_TIPO_INSCRICAO = 2


# ============================================================
# RETENTATIVA POR NOSSO NÚMERO DUPLICADO
#
# Se o BB recusar o boleto com o erro 4874915 (nosso número
# já existente), o item usado para montar o NOSSO NÚMERO é
# somado em 1 (sempre com 3 dígitos, zeros à esquerda) e o
# envio é refeito, até conseguir criar ou chegar no item 999.
#
# Apenas o numeroTituloCliente (nosso número) muda.
# O numeroTituloBeneficiario continua com o item original.
#
# Exemplo: 003 -> 004 -> 005 ...
# ============================================================

ITEM_MAXIMO = 999

# Limite de avanços do contador REN/REEMB quando o nosso
# número gerado já existir no BB (contador fora de sincronia).
REN_MAX_RETENTATIVAS = 50

CODIGOS_ERRO_NOSSO_NUMERO_DUPLICADO = {"4874915"}


def eh_erro_nosso_numero_duplicado(resposta):

    if resposta.status_code in (200, 201):
        return False

    try:
        dados = resposta.json()
    except ValueError:
        return False

    if not isinstance(dados, dict):
        return False

    erros = (
        dados.get("erros")
        or dados.get("errors")
        or []
    )

    for erro in erros:

        if not isinstance(erro, dict):
            continue

        codigo = str(
            erro.get("codigo")
            or erro.get("code")
            or ""
        ).strip()

        if codigo in CODIGOS_ERRO_NOSSO_NUMERO_DUPLICADO:
            return True

    return False


def somar_item(item):

    return str(int(item) + 1).zfill(3)


def gerar_numeros_titulo(
    document_reference,
    item,
    numero_convenio
):

    # ========================================================
    # NÚMERO DO TÍTULO BENEFICIÁRIO
    #
    # Regras:
    #
    # 1. REN.EM ANEXO:
    #    REN + contador com 5 dígitos
    #
    #    REN=1
    #    resultado: RENEG00001
    #
    # 2. REEMB. PLANO
    #    REN + contador com 5 dígitos
    #
    #    REN=1
    #    resultado: PSAUDE0001
    #
    # 3. ADT:
    #    mantém o valor original
    #
    #    ADT-123 + 003
    #    resultado: ADT-123003
    #
    # 4. Demais casos:
    #    remove tudo após o primeiro hífen
    #
    #    004356444-1 + 003
    #    resultado: 004400000012
    # ========================================================

    if document_reference.upper().startswith("REN. EM ANEXO"):

        numero_titulo_beneficiario = (
            "RENEG"
            + str(ren_contador).zfill(5)
        )

        # Incrementa para o próximo título
        proximo_ren = ren_contador + 1

        # REN não usa o item, então somar o item não muda nada
        usa_item = False

    elif document_reference.upper().startswith("REEMB. PLANO"):

        numero_titulo_beneficiario = (
            "PSAUDE"
            + str(ren_contador).zfill(4)
        )

        # Incrementa para o próximo título
        proximo_ren = ren_contador + 1
        
        # REN não usa o item, então somar o item não muda nada
        usa_item = False

    elif document_reference.upper().startswith("ADT"):

        referencia_sem_hifen = document_reference.strip()

        numero_titulo_beneficiario = (
                referencia_sem_hifen
                + item
            )

        proximo_ren = None

        usa_item = True

    else:
        referencia_sem_hifen = (
            document_reference.split("-", 1)[0].strip()
        )

        numero_titulo_beneficiario = (
            referencia_sem_hifen
            + item
        )

        proximo_ren = None

        usa_item = True


    # ========================================================
    # NÚMERO DO TÍTULO CLIENTE
    #
    # 000 + convênio + título beneficiário
    #
    # Exemplo BB003:
    #
    # 000 + 1234567 + 004400000012
    #
    # 0001234567004400000012
    # ========================================================

    numero_titulo_beneficiario_numerico = re.sub(
        r"\D",
        "0",
        str(numero_titulo_beneficiario)
    )

    numero_titulo_cliente = (
        "000"
        + str(numero_convenio)
        + (numero_titulo_beneficiario_numerico[-10:]).zfill(10))

    return (
        numero_titulo_beneficiario,
        numero_titulo_cliente,
        proximo_ren,
        usa_item
    )


# ============================================================
# FUNÇÃO DE CRIAÇÃO
# ============================================================

def criacaoBB(linha):

    # CORREÇÃO: o contador REN é atualizado em memória a cada
    # boleto aceito. Antes ele só era gravado no arquivo, então
    # todos os títulos REN/REEMB da mesma execução recebiam o
    # mesmo número (ex.: 4x PSAUDE0001) e só o primeiro passava.
    global ren_contador

    # ========================================================
    # HOUSE BANK
    # ========================================================

    house_bank = str(
        linha["HouseBank"]
    ).strip()

    configuracao = CONFIGURACAO_HOUSE_BANK.get(
        house_bank
    )

    if configuracao is None:

        raise ValueError(
            f"HouseBank não parametrizado: {house_bank}"
        )

    numero_convenio = configuracao["numeroConvenio"]

    numero_carteira = configuracao["numeroCarteira"]

    numero_variacao_carteira = configuracao[
        "numeroVariacaoCarteira"
    ]

    codigo_modalidade = configuracao[
        "codigoModalidade"
    ]

    BB_ClientID = configuracao["ClientID"]
    
    BB_ClientSecret = configuracao["ClientSecret"]
    
    BB_appKey = configuracao["appKey"]    
    
    # ========================================================
    # CAMPOS SAP
    # ========================================================

    company_code = str(
        linha["CompanyCode"]
    ).strip()

    accounting_document = str(
        linha["AccountingDocument"]
    ).strip()

    accounting_document_item = str(
        linha["AccountingDocumentItem"]
    ).strip()

    fiscal_year = str(
        linha["FiscalYear"]
    ).strip()

    document_reference = str(
        linha["DocumentReferenceID_2"]
    ).strip()

    customer = str(
        linha["Customer"]
    ).strip()

    bp_tax_type = str(
        linha["BPTaxType"]
    ).strip()

    bp_tax_number = str(
        linha["BPTaxNumber"]
    ).strip()


    # ========================================================
    # DATAS
    # ========================================================

    data_emissao = str(
        linha["Dt_lcto"]
    ).strip()

    data_vencimento = str(
        linha["NetDueDate"]
    ).strip()


    # ========================================================
    # VALOR
    # ========================================================

    valor_original = float(
        linha["AmountInCompanyCodeCurrency"]
    )


    # ========================================================
    # PAGADOR
    # ========================================================

    pagador_tipo_inscricao = {
        "BR1": 2,  # CNPJ
        "BR2": 1,  # CPF
    }.get(bp_tax_type)

    if pagador_tipo_inscricao is None:

        raise ValueError(
            f"BPTaxType inválido: {bp_tax_type}"
        )


    pagador_nome =unicodedata.normalize(
        "NFKD",
        str(linha["BusinessPartnerFullName"])
    ).encode(
        "ASCII",
        "ignore"
    ).decode(
        "ASCII"
    ).upper().strip()

    pagador_endereco = unicodedata.normalize(
        "NFKD",
        (
            f"{str(linha['StreetName']).strip()} "
            f"{str(linha['HouseNumber']).strip()}"
        )
    ).encode(
        "ASCII",
        "ignore"
    ).decode(
        "ASCII"
    ).upper().strip()


    pagador_cep = str(
        linha["PostalCode"]
    ).replace("-", "").strip()


    pagador_cidade = unicodedata.normalize(
        "NFKD",
        str(linha["CityName"]).strip()
    ).encode(
        "ASCII",
        "ignore"
    ).decode(
        "ASCII"
    ).upper().strip()


    pagador_bairro = unicodedata.normalize(
        "NFKD",
        str(linha["District"]).strip()
    ).encode(
        "ASCII",
        "ignore"
    ).decode(
        "ASCII"
    ).upper().strip()


    pagador_uf = str(
        linha["Region"]
    ).strip()


    # ========================================================
    # TELEFONE / E-MAIL
    # ========================================================

    telefone = linha.get("Phone")

    email = linha.get("EmailAddress")


    if telefone is not None:

        telefone = str(
            telefone
        ).strip()


    if email is not None:

        email = str(
            email
        ).strip()

    # ========================================================
    # NÚMERO DO TÍTULO BENEFICIÁRIO / CLIENTE
    # (regras na função gerar_numeros_titulo)
    # ========================================================

    item_atual = accounting_document_item

    (
        numero_titulo_beneficiario,
        numero_titulo_cliente,
        proximo_ren,
        usa_item
    ) = gerar_numeros_titulo(
        document_reference,
        item_atual,
        numero_convenio
    )


    # ========================================================
    # CAMPO UTILIZAÇÃO BENEFICIÁRIO
    #
    # Customer + AccountingDocument + FiscalYear
    #
    # 1000001
    # 9400000001
    # 2026
    #
    # resultado:
    # 100000194000000012026
    # ========================================================

    campo_utilizacao_beneficiario = (
        customer
        + accounting_document
        + fiscal_year
    ).zfill(24)


    # ========================================================
    # DADOS DO BENEFICIÁRIO FINAL
    # ========================================================

    dados_beneficiario = DEPARA_COMPANY_CODE.get(
        company_code
    )

    if dados_beneficiario is None:

        raise ValueError(
            f"CompanyCode não parametrizado no "
            f"DEPARA_COMPANY_CODE: {company_code}"
        )


    beneficiario_cnpj = dados_beneficiario["CNPJ"]

    beneficiario_nome = dados_beneficiario["Nome"]


    # ========================================================
    # PAYLOAD
    # ========================================================

    payload = {

        "numeroConvenio": numero_convenio,

        "numeroCarteira": numero_carteira,

        "numeroVariacaoCarteira":
            numero_variacao_carteira,

        "codigoModalidade": codigo_modalidade,

        "dataEmissao": data_emissao,

        "dataVencimento": data_vencimento,

        "valorOriginal": valor_original,

        "valorAbatimento":
            VALOR_ABATIMENTO,

        "quantidadeDiasProtesto":
            QUANTIDADE_DIAS_PROTESTO,

        "quantidadeDiasNegativacao":
            QUANTIDADE_DIAS_NEGATIVACAO,

        "orgaoNegativador":
            ORGAO_NEGATIVADOR,

        "indicadorAceiteTituloVencido":
            INDICADOR_ACEITE_TITULO_VENCIDO,

        "numeroDiasLimiteRecebimento":
            NUMERO_DIAS_LIMITE_RECEBIMENTO,

        "codigoAceite":
            CODIGO_ACEITE,

        "codigoTipoTitulo":
            CODIGO_TIPO_TITULO,

        "descricaoTipoTitulo":
            DESCRICAO_TIPO_TITULO,

        "indicadorPermissaoRecebimentoParcial":
            INDICADOR_PERMISSAO_RECEBIMENTO_PARCIAL,

        "numeroTituloBeneficiario":
            numero_titulo_beneficiario,

        "campoUtilizacaoBeneficiario":
            campo_utilizacao_beneficiario,

        "numeroTituloCliente":
            numero_titulo_cliente,

        "mensagemBloquetoOcorrencia":
            "",


        # ====================================================
        # DESCONTOS
        # ====================================================

        "desconto": {
            "tipo": 0,
            "dataExpiracao": None,
            "porcentagem": 0,
            "valor": 0.0
        },

        "segundoDesconto": {
            "dataExpiracao": None,
            "porcentagem": 0,
            "valor": 0
        },

        "terceiroDesconto": {
            "dataExpiracao": None,
            "porcentagem": 0,
            "valor": 0
        },


        # ====================================================
        # JUROS
        # ====================================================

        "jurosMora": {
            "tipo": JUROS_TIPO,
            "porcentagem": JUROS_PORCENTAGEM,
            "valor": JUROS_VALOR
        },


        # ====================================================
        # MULTA
        # ====================================================

        "multa": {
            "tipo": MULTA_TIPO,
            "data": MULTA_DATA,
            "porcentagem": MULTA_PORCENTAGEM,
            "valor": MULTA_VALOR
        },


        # ====================================================
        # PAGADOR
        # ====================================================

        "pagador": {

            "tipoInscricao":
                pagador_tipo_inscricao,

            "numeroInscricao":
                bp_tax_number,

            "nome":
                pagador_nome,

            "endereco":
                pagador_endereco,

            "cep":
                int(pagador_cep),

            "cidade":
                pagador_cidade,

            "bairro":
                pagador_bairro,

            "uf":
                pagador_uf,

            "telefone":
                telefone,

            "email":
                email
        },


        # ====================================================
        # BENEFICIÁRIO FINAL
        # ====================================================

        "beneficiarioFinal": {

            "tipoInscricao":
                BENEFICIARIO_TIPO_INSCRICAO,

            "numeroInscricao":
                beneficiario_cnpj,

            "nome":
                beneficiario_nome
        },


        # ====================================================
        # PIX
        # ====================================================

        "indicadorPix":
            INDICADOR_PIX,

        "idLocationPix":
            ID_LOCATION_PIX,

        "idLocationRecorrencia":
            ID_LOCATION_RECORRENCIA
    }


    # ========================================================
    # DEBUG
    # ========================================================

    print()
    print("=" * 70)
    print("BOLETO GERADO")
    print("=" * 70)

    print(
        f"HouseBank: {house_bank}"
    )

    print(
        f"Convênio: {numero_convenio}"
    )

    print(
        f"Cliente: {customer}"
    )

    print(
        f"Documento: {accounting_document}"
    )

    print(
        f"Item: {accounting_document_item}"
    )

    print(
        f"Valor: {valor_original}"
    )

    print(
        f"Vencimento: {data_vencimento}"
    )

    print(
        f"Número título beneficiário: "
        f"{numero_titulo_beneficiario}"
    )

    print(
        f"Número título cliente: "
        f"{numero_titulo_cliente}"
    )

    print()

    print(
        json.dumps(
            payload,
            indent=4,
            ensure_ascii=False
        )
    )

    # ========================================================
    # OBTER TOKEN BB
    # ========================================================

    token = obter_token(configuracao)


    # ========================================================
    # HEADERS API BB
    # ========================================================

    headers = {

        "Authorization":
            f"Bearer {token}",

        "Accept":
            "application/json",

        "Content-Type":
            "application/json"
    }

    params = {
        "gw-dev-app-key": BB_appKey
    }

    # ========================================================
    # ENVIO DO BOLETO
    # ========================================================

    print()
    print("=" * 70)
    print("ENVIANDO BOLETO PARA API BB")
    print("=" * 70)

    print(
        f"Endpoint: {BB_ENDPOINT_REGISTRO}"
    )


    tentativa = 1

    while True:

        print()
        print(
            f"Tentativa {tentativa} | Item: {item_atual} | "
            f"Nosso número: {numero_titulo_cliente}"
        )

        resposta = requests.post(

            BB_ENDPOINT_REGISTRO,

            params=params,

            headers=headers,

            json=payload,

            timeout=60
        )

        # Criou, ou deu um erro que não é de nosso número: segue o fluxo
        if not eh_erro_nosso_numero_duplicado(resposta):
            break

        print()
        print(
            f"Nosso número {numero_titulo_cliente} já existe no BB "
            f"(item {item_atual})."
        )
        print(resposta.text)

        # REN/REEMB não usam o item: em vez de somar o item,
        # avança o contador e remonta os dois números.
        if not usa_item:

            if tentativa >= REN_MAX_RETENTATIVAS:
                print(
                    f"Contador REN avançou {REN_MAX_RETENTATIVAS} vezes "
                    f"sem achar número livre. Abortando."
                )
                break

            ren_contador = proximo_ren

            (
                numero_titulo_beneficiario,
                numero_titulo_cliente,
                proximo_ren,
                _
            ) = gerar_numeros_titulo(
                document_reference,
                item_atual,
                numero_convenio
            )

            payload["numeroTituloBeneficiario"] = numero_titulo_beneficiario
            payload["numeroTituloCliente"] = numero_titulo_cliente

            print(
                f"Contador REN avançado: novo título "
                f"{numero_titulo_beneficiario}"
            )

            tentativa += 1
            continue

        if int(item_atual) >= ITEM_MAXIMO:
            print(
                f"Item chegou em {ITEM_MAXIMO}, "
                f"não é possível somar mais."
            )
            break

        # Soma 1 no item (3 dígitos) e remonta APENAS o nosso número.
        # O numeroTituloBeneficiario continua o original.
        item_atual = somar_item(item_atual)

        (
            _,
            numero_titulo_cliente,
            _,
            _
        ) = gerar_numeros_titulo(
            document_reference,
            item_atual,
            numero_convenio
        )

        payload["numeroTituloCliente"] = numero_titulo_cliente

        tentativa += 1

    # ========================================================
    # SALVAR PRÓXIMO REN
    #
    # Só avança o contador se o BB aceitou o boleto.
    # ========================================================

    if (
        proximo_ren is not None
        and resposta.status_code in (200, 201)
    ):

        salvar_ren(
            PARAMETROS,
            proximo_ren
        )

        # Atualiza também em memória para o próximo título
        # desta mesma execução.
        ren_contador = proximo_ren

        print()
        print(
            f"REN atualizado com sucesso: "
            f"{proximo_ren}"
        )

    # ========================================================
    # RESPOSTA API BB
    # ========================================================

    print()
    print("=" * 70)
    print("RESPOSTA API BB")
    print("=" * 70)

    print(
        f"HTTP STATUS: {resposta.status_code}"
    )

    print()

    try:

        resposta_json = resposta.json()

        print(
            json.dumps(
                resposta_json,
                indent=4,
                ensure_ascii=False
            )
        )

    except ValueError:

        resposta_json = None

        print(
            resposta.text
        )


    # ========================================================
    # RETORNO DA FUNÇÃO
    # ========================================================

    return {
        "status_code": resposta.status_code,
        "resposta": resposta_json,
        "payload": payload,
        "item_utilizado": item_atual,
        "tentativas": tentativa
    }