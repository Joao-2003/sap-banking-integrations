import base64
import json
import os
import re
import ssl
import time
import requests
import unicodedata
from datetime import datetime, timedelta
from requests.adapters import HTTPAdapter


# ============================================================
# ADAPTER DE SSL - RENEGOCIAÇÃO LEGADA
#
# O servidor do Banco ABC (atrás de Akamai) exige renegociação
# de TLS no meio da conexão. O curl/schannel (Windows) aceita
# isso automaticamente, mas o OpenSSL usado pelo módulo `ssl`
# do Python bloqueia renegociação legada por padrão a partir
# de versões recentes — a chamada simplesmente trava esperando
# resposta (visto no traceback em ssl.py / recv_into).
#
# OP_LEGACY_SERVER_CONNECT reabilita esse comportamento só
# para as chamadas ao ABC.
# ============================================================

class LegacyRenegotiationAdapter(HTTPAdapter):

    def init_poolmanager(self, *args, **kwargs):

        contexto = ssl.create_default_context()

        contexto.options |= ssl.OP_LEGACY_SERVER_CONNECT

        kwargs["ssl_context"] = contexto

        return super().init_poolmanager(*args, **kwargs)


sessao_abc = requests.Session()

sessao_abc.mount(
    "https://",
    LegacyRenegotiationAdapter()
)

# ============================================================
# CARREGAR PARÂMETROS
# ============================================================

PARAMETROS = "parametros_prod.txt"


def carregar_parametros(arquivo):

    parametros = {}

    with open(
        arquivo,
        "r",
        encoding="utf-8"
    ) as f:

        for linha in f:

            linha = linha.strip()

            if not linha:
                continue

            if linha.startswith("#"):
                continue

            if "=" not in linha:
                continue

            chave, valor = linha.split(
                "=",
                1
            )

            parametros[chave.strip()] = (
                valor.strip()
                .strip('"')
                .strip("'")
            )

    return parametros


parametros = carregar_parametros(
    PARAMETROS
)


# ============================================================
# BANCO ABC - HOMOLOGAÇÃO
# ============================================================

ABC_TOKEN_URL = parametros[
    "ABC_TOKEN_URL_PROD"
]

ABC_ENDPOINT_REGISTRO = parametros[
    "ABC_BOLETO_URL_PROD"
]

ABC_USERNAME = parametros[
    "ABC_USERNAME"
]

ABC_PASSWORD = parametros[
    "ABC_PASSWORD"
]

ABC_CODIGO_EMPRESA = parametros[
    "ABC_CODIGO_EMPRESA"
]

ABC_CLIENT_ID = parametros[
    "ABC_CLIENT_ID"
]

ABC_CLIENT_SECRET = parametros[
    "ABC_CLIENT_SECRET"
]


# ============================================================
# CONFIGURAÇÕES FIXAS ABC
# ============================================================

# CORREÇÃO: "0000" não é um código de banco válido.
# Todos os exemplos do manual e da coleção Postman usam
# "0246", que é o código do próprio Banco ABC Brasil.
COD_BANCO = "0246"

COD_ESPECIE_DOC = "02"
ACEITE = "NAO"

VLR_ABATIMENTO = 0.0


# ============================================================
# DESCONTOS
# ============================================================

COD_DESCONTO_1 = "0"
VLR_DESCONTO_1 = 0.0
TX_DESCONTO_1 = 0.0
DATA_DESCONTO_1 = ""

COD_DESCONTO_2 = "0"
VLR_DESCONTO_2 = 0.0
TX_DESCONTO_2 = 0.0
DATA_DESCONTO_2 = ""

COD_DESCONTO_3 = "0"
VLR_DESCONTO_3 = 0.0
TX_DESCONTO_3 = 0.0
DATA_DESCONTO_3 = ""


# ============================================================
# MULTA
# ============================================================

COD_MULTA = "0"
VLR_MULTA = 0.0
TX_MULTA = 0.0
DATA_MULTA = ""


# ============================================================
# MORA / JUROS
# ============================================================

COD_MORA = "2"
TX_MORA = 2.5
VLR_MORA = 0.0


# ============================================================
# AGENDA DE CARTÓRIO
# ============================================================

POSSUI_AGENDA = "SIM"
TIPO_AGENDAMENTO = "C"
CRITERIO_DIAS = "1"
NUM_DIAS_AGENDA = 9


# ============================================================
# PAGAMENTO PARCIAL
# ============================================================

IND_PAGTO_PARCIAL = "N"
QTD_PAGTOS_PARCIAIS = 0

TIPO_VLR_PERC_MINIMO = ""
VLR_PERC_MINIMO = 0.0

TIPO_VLR_PERC_MAXIMO = ""
VLR_PERC_MAXIMO = 0.0


# ============================================================
# MOEDA
# ============================================================

COD_INDICE = ""


# ============================================================
# SACADOR
# ============================================================

TIPO_PESSOA_SACADOR = ""
CNPJ_CPF_SACADOR = ""
NOME_SACADOR = ""
ENDERECO_SACADOR = ""
BAIRRO_SACADOR = ""
CEP_SACADOR = ""
CIDADE_SACADOR = ""
UF_SACADOR = ""


# ============================================================
# MENSAGENS
# ============================================================

MENSAGEM_1 = ""
MENSAGEM_2 = ""
MENSAGEM_3 = ""
MENSAGEM_4 = ""
MENSAGEM_5 = ""


CONFIGURACAO_HOUSE_BANK = {

    "ABC01": {
        "codCliente": "000000001",
        "numContaHeader": "S0000000000000000001",
        "numCarteira": "144"
    }
}


# ============================================================
# DE-PARA COMPANY CODE
# ============================================================

DEPARA_COMPANY_CODE = {

    "BR01": {

        "Nome": "EMPRESA A LTDA",

        "CNPJ": "11222333000181"

    },

    "BR02": {

        "Nome": "EMPRESA B LTDA",

        "CNPJ": "44555666000110"

    },

    "BR04": {

        "Nome": "EMPRESA C LTDA",

        "CNPJ": "77888999000155"

    }

}


# ============================================================
# CONTROLE DO CONTADOR REN (RENEGOCIAÇÃO)
#
# Usado apenas para o caso "REN. EM ANEXO", onde o
# numeroTituloBeneficiario é gerado sequencialmente
# (RENEG00001, RENEG00002, ...) em vez de vir do SAP.
#
# Segue o mesmo padrão de arquivo de controle já usado
# para outros bancos (ex.: nosso_numero_sofisa.txt).
# ============================================================

ARQUIVO_REN_CONTADOR = "ren_contador_abc.txt"

# Pasta dos PDFs: sempre ao lado deste script, independente
# de onde o terminal estiver quando o processador for executado.
PASTA_PDF_BOLETOS = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "boletos_abc_pdf"
)

# PDFs de boletos gerados nesta execução do processador. O processador
# lê esta lista no fim e envia todos num zip para o Slack.
PDFS_GERADOS_NESTA_EXECUCAO = []


def nome_arquivo_boleto(cliente, referencia, item):
    """
    Nome do PDF no mesmo padrão dos boletos Santander:
        AGRICOLA_AUTO_PECAS_LTDA_NF_36956-1.pdf
    cliente   : razão social (acentos e pontuação removidos, espaços viram _)
    referencia: DocumentReferenceID_2 (ex.: 001825121-1 -> NF 36956)
    item      : item contábil = parcela (ex.: 001 -> 1)
    """
    nome = re.sub(r"[^A-Z0-9]+", "_", texto_ascii(cliente)).strip("_")[:60].rstrip("_") or "CLIENTE"

    numero = str(referencia).split("-", 1)[0].strip()
    if numero.isdigit():
        nf = str(int(numero))
    else:
        nf = re.sub(r"[^A-Z0-9]+", "_", texto_ascii(referencia)).strip("_") or "SEM_REF"

    try:
        parcela = str(int(item))
    except (TypeError, ValueError):
        parcela = re.sub(r"\D", "", str(item)) or "1"

    return f"{nome}_NF_{nf}-{parcela}.pdf"


def carregar_ren_contador():

    try:

        with open(
            ARQUIVO_REN_CONTADOR,
            "r",
            encoding="utf-8"
        ) as arquivo:

            conteudo = arquivo.read().strip()

            return int(conteudo) if conteudo else 1

    except FileNotFoundError:

        return 1


def salvar_ren_contador(valor):

    with open(
        ARQUIVO_REN_CONTADOR,
        "w",
        encoding="utf-8"
    ) as arquivo:

        arquivo.write(
            str(valor)
        )


ren_contador = carregar_ren_contador()


# ============================================================
# FORMATO DE DATA EXIGIDO PELA API DO BANCO ABC
#
# O manual e a coleção Postman exigem o formato DD/MM/AAAA
# (com barra). O processador_ABC.py entrega as datas do SAP
# no formato DD.MM.AAAA (com ponto), então é preciso
# converter antes de montar o payload.
# ============================================================

def formatar_data_abc(data_str):

    return (
        datetime.strptime(
            data_str,
            "%d.%m.%Y"
        )
        .strftime("%d/%m/%Y")
    )


# ============================================================
# LIMITE DE TAMANHO DO NOSSO NÚMERO
#
# O manual do Banco ABC define nossoNumero como Alfanumérico
# de no máximo 11 posições. As regras de REN/ADT/demais casos
# podem gerar valores maiores que isso (ex.: referência SAP
# com zeros de preenchimento + item), então aplicamos um
# ajuste de segurança:
#
# 1. Remove zeros à esquerda (são apenas preenchimento de
#    largura fixa do SAP, não carregam informação).
#
# 2. Se, mesmo assim, ainda ultrapassar 11 caracteres, mantém
#    os caracteres mais à direita (a parte mais específica/
#    única do identificador).
# ============================================================

TAMANHO_MAXIMO_NOSSO_NUMERO = 11

# seuNumero: Alfanumérico de 15 posições (manual, item 3.4)
TAMANHO_MAXIMO_SEU_NUMERO = 15


# ============================================================
# TAMANHOS MÁXIMOS DO PAGADOR (manual, item 3.4)
#
# Acima disso o banco recusa com 51067 ("Um campo ultrapassou
# o tamanho máximo"). Cortamos antes de enviar.
# ============================================================

TAMANHOS_PAGADOR = {
    "nome": 40,
    "endereco": 40,
    "bairro": 20,
    "cidade": 20,
    "email": 50,
    "telefone": 9,
}


def limitar(valor, campo):

    limite = TAMANHOS_PAGADOR[campo]

    if len(valor) > limite:

        print(
            f"AVISO: {campo} com {len(valor)} caracteres "
            f"cortado para {limite}: '{valor[:limite]}'"
        )

        return valor[:limite]

    return valor


def ajustar_tamanho_nosso_numero(
    valor,
    tamanho_maximo=TAMANHO_MAXIMO_NOSSO_NUMERO
):

    if len(valor) <= tamanho_maximo:
        return valor

    sem_zeros_esquerda = valor.lstrip("0") or "0"

    if len(sem_zeros_esquerda) <= tamanho_maximo:
        return sem_zeros_esquerda

    return sem_zeros_esquerda[-tamanho_maximo:]


# ============================================================
# TEXTO LIMPO
#
# Campos vazios vindos do pandas chegam como NaN/None, e
# str(NaN) vira o texto "nan" -- que estava indo para o banco
# no campo "email". Esta função devolve "" nesses casos.
# ============================================================

def texto_limpo(valor):

    if valor is None:
        return ""

    try:
        if valor != valor:  # NaN
            return ""
    except Exception:
        pass

    texto = str(valor).strip()

    if texto.lower() in ("nan", "none", "nat"):
        return ""

    return texto


def texto_ascii(valor):

    return unicodedata.normalize(
        "NFKD",
        texto_limpo(valor)
    ).encode(
        "ASCII",
        "ignore"
    ).decode(
        "ASCII"
    ).upper().strip()


# ============================================================
# OBTER TOKEN ABC
# ============================================================

_token_cache = {
    "token": None,
    "expira_em": 0.0
}

# Renova o token quando faltar menos que isso para expirar
MARGEM_RENOVACAO_TOKEN_S = 120


def obter_token(forcar=False):
    """Devolve o token em cache; só pede um novo ao banco se
    ainda não houver, se estiver perto de expirar, ou se
    forcar=True (usado quando o banco responde 401)."""

    agora = time.time()

    if (
        not forcar
        and _token_cache["token"]
        and agora < _token_cache["expira_em"] - MARGEM_RENOVACAO_TOKEN_S
    ):
        return _token_cache["token"]

    token, validade = _solicitar_token()

    _token_cache["token"] = token
    _token_cache["expira_em"] = agora + validade

    return token


# Se o banco continuar recusando token reaproveitado, mude
# para True: volta a pedir um token novo a cada boleto.
TOKEN_POR_BOLETO = False

CODIGOS_TOKEN_RECUSADO = {"50252"}


def _token_recusado(resposta):

    if resposta.status_code == 401:
        return True

    try:
        corpo = resposta.json()
    except ValueError:
        return False

    if not isinstance(corpo, dict) or corpo.get("sucesso") is True:
        return False

    codigo = str(
        (corpo.get("desenvolvedor") or {}).get("codigoRetorno", "")
    ).strip()

    return codigo in CODIGOS_TOKEN_RECUSADO


def _solicitar_token():

    print()
    print("=" * 70)
    print("AUTENTICAÇÃO BANCO ABC")
    print("=" * 70)

    headers = {
        "Content-Type": "application/x-www-form-urlencoded",
        "Accept": "application/json"
    }

    data = {
        "username": ABC_USERNAME,
        "password": ABC_PASSWORD,
        "codigoEmpresa": ABC_CODIGO_EMPRESA,
        "client_id": ABC_CLIENT_ID,
        "client_secret": ABC_CLIENT_SECRET,
        "grant_type": "password"
    }

    resposta = sessao_abc.post(
        ABC_TOKEN_URL,
        headers=headers,
        data=data,
        timeout=30
    )

    print(
        f"HTTP STATUS TOKEN: {resposta.status_code}"
    )

    print(
        "URL TOKEN:",
        ABC_TOKEN_URL
    )

    if resposta.status_code != 200:

        print("\nERRO ABC:")
        print(resposta.text)

        resposta.raise_for_status()

    dados = resposta.json()

    token = dados.get("access_token")

    if not token:

        raise ValueError(
            "ABC não retornou access_token."
        )

    print(
        "Access token obtido com sucesso."
    )

    print(
        "Token:",
        token[:10] + "..." + token[-5:]
    )

    print(
        "Validade:",
        dados.get("expires_in"),
        "segundos"
    )

    try:
        validade = int(dados.get("expires_in") or 0)
    except (TypeError, ValueError):
        validade = 0

    # Sem validade informada: assume 5 minutos, por segurança
    return token, (validade or 300)


# ============================================================
# FUNÇÃO DE CRIAÇÃO DO BOLETO ABC
# ============================================================

def criacaoABC(linha):

    global ren_contador

    # ========================================================
    # HOUSE BANK
    # ========================================================

    house_bank = str(
        linha["HouseBank"]
    ).strip()


    configuracao = (
        CONFIGURACAO_HOUSE_BANK.get(
            house_bank
        )
    )


    if configuracao is None:

        raise ValueError(
            f"HouseBank não parametrizado: "
            f"{house_bank}"
        )


    # ========================================================
    # DADOS DA CONFIGURAÇÃO
    # ========================================================

    cod_cliente = configuracao[
        "codCliente"
    ]

    num_conta_header = configuracao[
        "numContaHeader"
    ]

    num_carteira = configuracao[
        "numCarteira"
    ]


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
    #
    # CORREÇÃO: as datas chegam do processador no formato
    # DD.MM.AAAA (ponto). A API do Banco ABC exige DD/MM/AAAA
    # (barra) — ver manual, itens 3.4 e exemplos Postman.
    # Sem essa conversão, toda chamada retorna o erro 51062
    # (DATA INVÁLIDA).
    # ========================================================

    data_emissao_raw = str(
        linha["Dt_lcto"]
    ).strip()

    data_vencimento_raw = str(
        linha["NetDueDate"]
    ).strip()

    data_emissao = formatar_data_abc(
        data_emissao_raw
    )

    data_vencimento = formatar_data_abc(
        data_vencimento_raw
    )


    # ========================================================
    # VALOR
    # ========================================================

    valor_original = float(
        linha["AmountInCompanyCodeCurrency"]
    )


    # ========================================================
    # TIPO DE PESSOA
    # ========================================================

    tipo_pessoa = {

        "BR1": "2",  # CNPJ

        "BR2": "1"   # CPF

    }.get(
        bp_tax_type
    )


    if tipo_pessoa is None:

        raise ValueError(
            f"BPTaxType inválido: "
            f"{bp_tax_type}"
        )


    # ========================================================
    # PAGADOR
    # ========================================================

    pagador_nome = texto_ascii(
        linha["BusinessPartnerFullName"]
    )

    pagador_endereco = texto_ascii(
        f"{texto_limpo(linha['StreetName'])} "
        f"{texto_limpo(linha['HouseNumber'])}"
    )

    pagador_bairro = texto_ascii(
        linha["District"]
    )

    pagador_cidade = texto_ascii(
        linha["CityName"]
    )

    pagador_uf = texto_limpo(
        linha["Region"]
    )

    pagador_cep = texto_limpo(
        linha["PostalCode"]
    )


    # ========================================================
    # TELEFONE / EMAIL
    #
    # CORREÇÃO: NaN do pandas virava "nan" e era enviado ao
    # banco como e-mail do pagador.
    # ========================================================

    telefone = texto_limpo(
        linha.get("Phone", "")
    )

    email = texto_limpo(
        linha.get("EmailAddress", "")
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

    campo_livre = (
        customer
        + accounting_document
        + fiscal_year
    ).zfill(25)

    # ========================================================
    # NÚMERO DO TÍTULO BENEFICIÁRIO (nossoNumero)
    #
    # Regras:
    #
    # 1. REN. EM ANEXO:
    #    REN + contador com 5 dígitos
    #
    #    REN=1
    #    resultado: RENEG00001
    #
    # 2. ADT:
    #    mantém o valor original
    #
    #    ADT-123 + 003
    #    resultado: ADT-123003
    #
    # 3. Demais casos:
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

    elif document_reference.upper().startswith("ADT"):

        referencia_sem_hifen = document_reference.strip()

        numero_titulo_beneficiario = (
            referencia_sem_hifen
            + accounting_document_item
        )

        proximo_ren = None

    else:

        referencia_sem_hifen = (
            document_reference.split("-", 1)[0].strip()
        )

        numero_titulo_beneficiario = (
            referencia_sem_hifen
            + accounting_document_item
        )

        proximo_ren = None


    # ========================================================
    # SEU NÚMERO -- ÚNICO POR PARCELA
    #
    # CORREÇÃO: antes o seuNumero era só a referência SAP
    # ("001234567-8" -> "0012345678"), IGUAL para todas as
    # parcelas do título. O banco devolvia o mesmo boleto para
    # todas. Agora usamos o identificador por parcela
    # (referência + item), sem caracteres especiais e já no
    # tamanho máximo aceito:
    #
    #   001234567-8 + 002  ->  001234567802  ->  40000002
    #   ADT-123 + 003      ->  ADT123003
    #   REN. EM ANEXO      ->  RENEG00001
    # ========================================================

    numero_titulo_beneficiario = re.sub(
        r"[^A-Za-z0-9]",
        "",
        numero_titulo_beneficiario
    )

    seu_numero = ajustar_tamanho_nosso_numero(
        numero_titulo_beneficiario,
        TAMANHO_MAXIMO_SEU_NUMERO
    )

    if seu_numero != numero_titulo_beneficiario:

        print(
            f"seuNumero: '{numero_titulo_beneficiario}' "
            f"({len(numero_titulo_beneficiario)} caracteres) "
            f"ajustado para '{seu_numero}' "
            f"(limite {TAMANHO_MAXIMO_SEU_NUMERO})."
        )


    # ========================================================
    # PERSISTIR CONTADOR REN, SE FOI UTILIZADO
    # ========================================================

    if proximo_ren is not None:

        ren_contador = proximo_ren

        salvar_ren_contador(
            ren_contador
        )


    # nossoNumero vazio: o próprio banco gera.
    nosso_numero = ""


    # ========================================================
    # SACADOR
    # ========================================================

    # No seu cenário atual não estamos usando sacador.
    #
    # Portanto os campos permanecem vazios.
    #
    # ========================================================


    # ========================================================
    # PAYLOAD ABC
    # ========================================================

    payload = {

        "codCliente":
            str(cod_cliente).zfill(9),

        "numContaHeader":
            num_conta_header,

        "numCarteira":
            str(num_carteira).zfill(3),

        "nossoNumero":
            nosso_numero,

        "codBanco":
            COD_BANCO,

        "codModalBancos":
            "",

        "nossoNumeroBancos":
            "",

        "codEspecieDoc":
            COD_ESPECIE_DOC,

        "vlrNominal":
            valor_original,

        "vlrAbatimento":
            VLR_ABATIMENTO,

        "dataEmissao":
            data_emissao,

        "dataVencimento":
            data_vencimento,

        "seuNumero":
            seu_numero,

        "aceite":
            ACEITE,

        "cnpjCPF":
            bp_tax_number,

        "tipoPessoa":
            tipo_pessoa,

        "nome":
            limitar(pagador_nome, "nome"),

        "endereco":
            limitar(pagador_endereco, "endereco"),

        "bairro":
            limitar(pagador_bairro, "bairro"),

        "cidade":
            limitar(pagador_cidade, "cidade"),

        "uf":
            pagador_uf,

        "cep":
            pagador_cep,

        "email":
            limitar(email, "email"),

        "ddd":
            "",

        "telefone":
            limitar(telefone, "telefone"),

        "campoLivre":
            campo_livre,


        # ====================================================
        # SACADOR
        # ====================================================

        "tipoPessoaSacador":
            TIPO_PESSOA_SACADOR,

        "cnpjCPFSacador":
            CNPJ_CPF_SACADOR,

        "nomeSacador":
            NOME_SACADOR,

        "enderecoSacador":
            ENDERECO_SACADOR,

        "bairroSacador":
            BAIRRO_SACADOR,

        "cepSacador":
            CEP_SACADOR,

        "cidadeSacador":
            CIDADE_SACADOR,

        "ufSacador":
            UF_SACADOR,


        # ====================================================
        # MENSAGENS
        # ====================================================

        "mensagem1":
            MENSAGEM_1,

        "mensagem2":
            MENSAGEM_2,

        "mensagem3":
            MENSAGEM_3,

        "mensagem4":
            MENSAGEM_4,

        "mensagem5":
            MENSAGEM_5,


        # ====================================================
        # DESCONTO 1
        # ====================================================

        "codDesconto1":
            COD_DESCONTO_1,

        "vlrDesconto1":
            VLR_DESCONTO_1,

        "txDesconto1":
            TX_DESCONTO_1,

        "dataDesconto1":
            DATA_DESCONTO_1,


        # ====================================================
        # DESCONTO 2
        # ====================================================

        "codDesconto2":
            COD_DESCONTO_2,

        "vlrDesconto2":
            VLR_DESCONTO_2,

        "txDesconto2":
            TX_DESCONTO_2,

        "dataDesconto2":
            DATA_DESCONTO_2,


        # ====================================================
        # DESCONTO 3
        # ====================================================

        "codDesconto3":
            COD_DESCONTO_3,

        "vlrDesconto3":
            VLR_DESCONTO_3,

        "txDesconto3":
            TX_DESCONTO_3,

        "dataDesconto3":
            DATA_DESCONTO_3,


        # ====================================================
        # MULTA
        # ====================================================

        "codMulta":
            COD_MULTA,

        "dataMulta":
            DATA_MULTA,

        "txMulta":
            TX_MULTA,

        "vlrMulta":
            VLR_MULTA,


        # ====================================================
        # MORA
        #
        # CORREÇÃO: formatar em DD/MM/AAAA (barra), igual às
        # demais datas enviadas à API do Banco ABC.
        # ====================================================

        "codMora":
            COD_MORA,

        "dataMora":
            (
                datetime.strptime(data_vencimento_raw, "%d.%m.%Y")
                + timedelta(days=1)
            ).strftime("%d/%m/%Y"),

        "txMora":
            TX_MORA,

        "vlrMora":
            VLR_MORA,


        # ====================================================
        # AGENDA DE CARTÓRIO
        # ====================================================

        "possuiAgenda":
            POSSUI_AGENDA,

        "tipoAgendamento":
            TIPO_AGENDAMENTO,

        "criterioDias":
            CRITERIO_DIAS,

        "numDiasAgenda":
            NUM_DIAS_AGENDA,


        # ====================================================
        # MOEDA
        # ====================================================

        "codIndice":
            COD_INDICE,


        # ====================================================
        # PAGAMENTO PARCIAL
        # ====================================================

        "indPagtoParcial":
            IND_PAGTO_PARCIAL,

        "qtdPagtosParciais":
            QTD_PAGTOS_PARCIAIS,

        "tipoVlrPercMinimo":
            TIPO_VLR_PERC_MINIMO,

        "vlrPercMinimo":
            VLR_PERC_MINIMO,

        "tipoVlrPercMaximo":
            TIPO_VLR_PERC_MAXIMO,

        "vlrPercMaximo":
            VLR_PERC_MAXIMO,


        # ====================================================
        # CONTROLE
        # ====================================================

        "tipoAutRecDivergente":
            "3"
    }


    # ========================================================
    # DEBUG
    # ========================================================

    print()
    print("=" * 70)
    print("BOLETO ABC GERADO")
    print("=" * 70)

    print(
        f"HouseBank: {house_bank}"
    )

    print(
        f"Cliente ABC: {cod_cliente}"
    )

    print(
        f"Documento SAP: "
        f"{accounting_document}"
    )

    print(
        f"Item SAP: "
        f"{accounting_document_item}"
    )

    print(
        f"Customer: "
        f"{customer}"
    )

    print(
        f"Valor: "
        f"{valor_original}"
    )

    print(
        f"Vencimento: "
        f"{data_vencimento}"
    )

    print(
        f"Seu Número: "
        f"{seu_numero}"
    )

    print(
        f"Nosso Número: "
        f"{nosso_numero}"
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
    # POST (token reaproveitado entre os boletos)
    # ========================================================

    print()
    print("=" * 70)
    print("ENVIANDO BOLETO PARA BANCO ABC")
    print("=" * 70)

    print(
        f"Endpoint: "
        f"{ABC_ENDPOINT_REGISTRO}"
    )

    def _enviar(token):

        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json"
        }

        return sessao_abc.post(
            ABC_ENDPOINT_REGISTRO,
            headers=headers,
            json=payload,
            timeout=60
        )

    resposta = _enviar(obter_token(forcar=TOKEN_POR_BOLETO))

    # --------------------------------------------------------
    # Token recusado: renova UMA vez e reenvia.
    #
    # O ABC sinaliza isso de dois jeitos:
    #   - HTTP 401;
    #   - HTTP 200 com sucesso=false e codigoRetorno 50252
    #     ("Senha informada é inválida ... Controle de Acesso"),
    #     visto em homologação depois de alguns boletos com o
    #     mesmo token. Nesse caso o boleto NÃO foi gerado, então
    #     reenviar é seguro (não duplica).
    # --------------------------------------------------------

    if _token_recusado(resposta):

        print(
            "Token recusado pelo banco "
            f"(HTTP {resposta.status_code}). Renovando e reenviando..."
        )

        resposta = _enviar(obter_token(forcar=True))


    # ========================================================
    # RESPOSTA
    # ========================================================

    print()
    print("=" * 70)
    print("RESPOSTA BANCO ABC")
    print("=" * 70)

    print(
        f"HTTP STATUS: "
        f"{resposta.status_code}"
    )

    print()


    pdf_arquivo = None
    pix = None

    try:

        resposta_json = resposta.json()

    except ValueError:

        resposta_json = None


    if isinstance(resposta_json, dict):

        # ----------------------------------------------------
        # PDF: salva em arquivo em vez de despejar o base64
        # inteiro no log
        # ----------------------------------------------------

        pdf_base64 = resposta_json.get("pdfBinario")

        if pdf_base64:

            try:

                os.makedirs(PASTA_PDF_BOLETOS, exist_ok=True)

                pdf_arquivo = os.path.join(
                    PASTA_PDF_BOLETOS,
                    nome_arquivo_boleto(
                        linha["BusinessPartnerFullName"],
                        document_reference,
                        accounting_document_item,
                    )
                )

                with open(pdf_arquivo, "wb") as arquivo_pdf:

                    arquivo_pdf.write(
                        base64.b64decode(pdf_base64)
                    )

                print(f"PDF salvo em: {pdf_arquivo}")

                PDFS_GERADOS_NESTA_EXECUCAO.append(pdf_arquivo)

            except Exception as erro_pdf:

                pdf_arquivo = None

                print(f"AVISO: não foi possível salvar o PDF: {erro_pdf}")

        # ----------------------------------------------------
        # PIX (Bolepix)
        #
        # O manual não documenta o objeto "pix" -- ele só vem
        # preenchido quando a carteira/conta está habilitada
        # para Bolepix no banco. Guardamos o que vier, sem
        # supor nomes de campos, para o relatório e o Slack.
        # ----------------------------------------------------

        pix_resposta = resposta_json.get("pix")

        if isinstance(pix_resposta, dict) and pix_resposta:

            pix = pix_resposta

            try:

                os.makedirs(PASTA_PDF_BOLETOS, exist_ok=True)

                pix_arquivo = os.path.join(
                    PASTA_PDF_BOLETOS,
                    f"{company_code}_{accounting_document}_"
                    f"{fiscal_year}_{accounting_document_item}_pix.json"
                )

                with open(pix_arquivo, "w", encoding="utf-8") as arquivo_pix:

                    json.dump(pix, arquivo_pix, indent=4, ensure_ascii=False)

                print(f"Dados Pix salvos em: {pix_arquivo}")

            except Exception as erro_pix:

                print(f"AVISO: não foi possível salvar o Pix: {erro_pix}")

        else:

            print("Pix: não retornado (Bolepix não habilitado nesta carteira/conta).")

        resposta_log = {
            chave: (
                f"<{len(valor)} caracteres omitidos>"
                if chave == "pdfBinario" and valor
                else valor
            )
            for chave, valor in resposta_json.items()
        }

        print(
            json.dumps(
                resposta_log,
                indent=4,
                ensure_ascii=False
            )
        )

    else:

        print(
            resposta.text
        )


    # ========================================================
    # RETORNO
    # ========================================================

    return {

        "status_code":
            resposta.status_code,

        "resposta":
            resposta_json,

        "payload":
            payload,

        "pdf_arquivo":
            pdf_arquivo,

        "pix":
            pix
    }