import requests
import pandas as pd
import unicodedata
import os
import json
import re
import zipfile
import xml.etree.ElementTree as ET
from datetime import date, timedelta, datetime
from io import BytesIO

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

SAP_API_1_URL = parametros["SAP_1_URL"]
SAP_API_1_USUARIO = parametros["SAP_1_USUARIO"]
SAP_API_1_SENHA = parametros["SAP_1_SENHA"]

SAP_API_2_URL = parametros["SAP_2_URL"]
SAP_API_2_USUARIO = parametros["SAP_2_USUARIO"]
SAP_API_2_SENHA = parametros["SAP_2_SENHA"]


# ============================================================
# CONTROLE DE TÍTULOS PROCESSADOS
# ============================================================

# ── mesmo arquivo e colunas usados pelo criar_boleto_BB / SAFRA ──
ARQUIVO_TITULOS_PROCESSADOS = "titulos_processados.csv"

COLUNAS_HISTORICO = [
    "Chave_Titulo",
    "HouseBank",
    "Data_Processamento"
]

def montar_chave_titulo(row):
    return "|".join([
        str(row["CompanyCode"]).strip(),
        str(row["AccountingDocument"]).strip(),
        str(row["FiscalYear"]).strip(),
        str(row["AccountingDocumentItem"]).strip()
    ])

def carregar_titulos_processados():
    """
    Lê titulos_processados.csv (compartilhado com BB e SAFRA) e devolve:
      - historico_df  : DataFrame completo (para concatenar depois)
      - titulos_set   : set com as Chave_Titulo já registradas
    """
    try:
        historico_df = pd.read_csv(
            ARQUIVO_TITULOS_PROCESSADOS,
            dtype=str
        )

        # Garantir todas as colunas caso o CSV esteja incompleto
        for coluna in COLUNAS_HISTORICO:
            if coluna not in historico_df.columns:
                historico_df[coluna] = ""

        historico_df = historico_df[COLUNAS_HISTORICO].fillna("")

    except FileNotFoundError:
        historico_df = pd.DataFrame(columns=COLUNAS_HISTORICO)

    titulos_set = set(
        historico_df["Chave_Titulo"].astype(str).str.strip()
    )

    return historico_df, titulos_set

def salvar_titulos_processados(historico_df, novos_registros):
    """
    Recebe o DataFrame histórico já carregado e uma lista de dicts
    com {Chave_Titulo, HouseBank, Data_Processamento}.
    Faz concat, deduplica pela Chave_Titulo (keep='last') e salva.
    """
    if not novos_registros:
        return historico_df  # nada a gravar

    novos_df = pd.DataFrame(novos_registros, columns=COLUNAS_HISTORICO)

    historico_atualizado = pd.concat(
        [historico_df, novos_df],
        ignore_index=True
    )

    historico_atualizado = (
        historico_atualizado
        .drop_duplicates(subset=["Chave_Titulo"], keep="last")
    )

    historico_atualizado.to_csv(
        ARQUIVO_TITULOS_PROCESSADOS,
        index=False,
        encoding="utf-8-sig"
    )

    return historico_atualizado

def obter_proximo_nsa(company_code):

    arquivo_seq = "sequencial_sofisa.txt"

    # ========================================================
    # LER SEQUENCIAIS EXISTENTES
    # ========================================================

    sequenciais = {}

    if os.path.exists(arquivo_seq):

        with open(
            arquivo_seq,
            "r",
            encoding="utf-8"
        ) as f:

            for linha in f:

                linha = linha.strip()

                if not linha or "=" not in linha:
                    continue

                empresa, numero = linha.split("=", 1)

                empresa = empresa.strip()
                numero = numero.strip()

                if numero.isdigit():
                    sequenciais[empresa] = int(numero)

    # ========================================================
    # PEGAR PRÓXIMO NÚMERO DA EMPRESA
    # ========================================================

    numero = sequenciais.get(
        company_code,
        1
    )

    # ========================================================
    # ATUALIZAR
    # ========================================================

    sequenciais[company_code] = numero + 1

    # ========================================================
    # SALVAR ARQUIVO
    # ========================================================

    with open(
        arquivo_seq,
        "w",
        encoding="utf-8"
    ) as f:

        for empresa, valor in sequenciais.items():

            f.write(
                f"{empresa}={valor}\n"
            )

    return numero

# ============================================================
# CONTROLE SEQUENCIAL DE NOSSO NÚMERO (por faixa liberada pelo banco)
# ============================================================

ARQUIVO_NOSSO_NUMERO = "nosso_numero_sofisa.txt"

def _ler_nosso_numero_controle():
    controle = {}

    if not os.path.exists(ARQUIVO_NOSSO_NUMERO):
        return controle

    with open(ARQUIVO_NOSSO_NUMERO, "r", encoding="utf-8") as f:
        for linha in f:
            linha = linha.strip()

            if not linha or "=" not in linha:
                continue

            empresa, numero = linha.split("=", 1)
            empresa = empresa.strip()
            numero = numero.strip()

            if numero.isdigit():
                controle[empresa] = int(numero)

    return controle

def _salvar_nosso_numero_controle(controle):
    with open(ARQUIVO_NOSSO_NUMERO, "w", encoding="utf-8") as f:
        for empresa, valor in controle.items():
            f.write(f"{empresa}={valor}\n")

def obter_proximo_nosso_numero(company_code):
    """
    Nosso Número no Sofisa NÃO pode ser derivado livremente do
    documento SAP: o banco reserva uma faixa fixa por convênio
    (ex.: 0074021814 a 0001256579) e rejeita qualquer valor fora
    dela. Por isso mantemos um contador persistido por empresa,
    dentro da faixa cadastrada em DEPARA_COMPANY_CODE.
    """

    empresa = DEPARA_COMPANY_CODE.get(company_code)

    if empresa is None:
        raise ValueError(f"CompanyCode não parametrizado: {company_code}")

    inicio = empresa.get("nosso_numero_inicio")
    fim = empresa.get("nosso_numero_fim")

    if inicio is None or fim is None:
        raise ValueError(
            f"Faixa de Nosso Número não cadastrada para {company_code}. "
            "Solicite ao Sofisa a faixa liberada para este convênio e "
            "preencha 'nosso_numero_inicio'/'nosso_numero_fim' em "
            "DEPARA_COMPANY_CODE."
        )

    controle = _ler_nosso_numero_controle()

    proximo = controle.get(company_code, inicio)

    if proximo > fim:
        raise ValueError(
            f"Faixa de Nosso Número esgotada para {company_code} "
            f"({inicio} a {fim}). Solicite uma nova faixa ao Sofisa."
        )

    controle[company_code] = proximo + 1
    _salvar_nosso_numero_controle(controle)

    return str(proximo).zfill(10)


def calcular_dv_nosso_numero(agencia, carteira, nosso_numero):
    """
    DV (dígito verificador) do Nosso Número, calculado por módulo 10,
    conforme manual do Sofisa.

    Regra:
    - Concatena-se Agência (SEM o dígito verificador da agência) +
      Carteira + Nosso Número.
    - Cada algarismo do número formado é multiplicado, da direita
      para a esquerda, pela sequência alternada 2, 1, 2, 1, 2...
    - Somam-se os algarismos de cada produto individualmente
      (ex.: produto 12 soma 1 + 2 = 3, não 12).
    - O total é dividido por 10; o DV é 10 menos o resto.
    - Se o resto for 0, o DV é 0.

    Exemplo do manual (deve retornar "9"):
        calcular_dv_nosso_numero("0001", "110", "1234567890") -> "9"
    """

    agencia = str(agencia).strip()
    carteira = str(carteira).strip()
    nosso_numero = str(nosso_numero).strip()

    numero_completo = agencia + carteira + nosso_numero

    soma = 0

    for indice, digito in enumerate(reversed(numero_completo)):

        multiplicador = 2 if indice % 2 == 0 else 1

        produto = int(digito) * multiplicador

        # soma os algarismos do produto individualmente (ex.: 12 -> 1+2)
        soma += sum(int(algarismo) for algarismo in str(produto))

    resto = soma % 10

    dv = 0 if resto == 0 else 10 - resto

    return str(dv)


# ============================================================
# DE-PARA COMPANY CODE -> DADOS BB
# ============================================================

DEPARA_COMPANY_CODE = {
    "BR01": {
        "Nome": "EMPRESA A LTDA",
        "CNPJ": "11222333000181",
        "BB": "sofisa",
        "ag": "00001",
        "cc": "000000000000001",
        "convenio": "000000001",
        "nosso_numero_inicio": 1000000,
        "nosso_numero_fim": 1999999,
    },
    "BR02": {
        "Nome": "EMPRESA B LTDA",
        "CNPJ": "44555666000110",
        "BB": "sofisa",
        "ag": "00001",
        "cc": "000000000000001",
        "convenio": "000000001",
        "nosso_numero_inicio": 1000000,
        "nosso_numero_fim": 1999999,
    },
    "BR04": {
        "Nome": "EMPRESA C LTDA",
        "CNPJ": "77888999000155",
        "BB": "sofisa",
        "ag": "00001",
        "cc": "000000000000001",
        "convenio": "000000001",
        "nosso_numero_inicio": 1000000,
        "nosso_numero_fim": 1999999,
    }
}

# ========================================================
# SLACK
# ========================================================
SLACK_URL = parametros["SLACK_URL"]
SLACK_CANAL = parametros["SLACK_CANAL"]
SLACK_TOKEN = parametros["SLACK_TOKEN"]

# ============================================================
# CONFIGURAÇÕES FIXAS - Sofisa
# ============================================================
CODIGO_BANCO = "637"
TIPO_INSCRICAO = "2"  # CNPJ
CODIGO_ARQUIVO = "1"  # 1 = Remessa
LAYOUT_ARQUIVO = "040"
NOME_BANCO = "BANCO SOFISA S.A."

# ============================================================
# DATA D
# ============================================================

data_d1 = date.today() - timedelta(days=0)

# AssignmentReference está no formato DD/MM/AAAA
data_d1_str = data_d1.strftime("%d/%m/%Y")

print(f"Data D-1 utilizada: {data_d1_str}")


# ============================================================
# HEADERS
# ============================================================

headers = {
    "Accept": "application/json"
}


# ============================================================
# ============================================================
# API 1 - TÍTULOS DE CLIENTES
# ============================================================
# ============================================================


# ============================================================
# FILTRO ODATA
# ============================================================

filtro = (
    "Customer ne '' "
    "and ClearingDate eq null "
    "and AccountingDocumentType eq 'RV' "
    f"and AssignmentReference eq 'SOFISA-{data_d1_str}' "
    #"and substringof('SOF', HouseBank) eq true"
)


# ============================================================
# COLUNAS NECESSÁRIAS
# ============================================================

select = ",".join([
    "CompanyCode",
    "AccountingDocument",
    "FiscalYear",
    "AccountingDocumentItem",
    "DocumentReferenceID_2",
    "CompanyCodeCurrency",
    "AmountInCompanyCodeCurrency",
    "Dt_lcto",
    "NetDueDate",
    "Customer",
    "AssignmentReference",
    "HouseBank"
])


# ============================================================
# PARÂMETROS ODATA
# ============================================================

params = {
    "$filter": filtro,
    "$select": select,
    "$format": "json"
}


# ============================================================
# REQUISIÇÃO
# ============================================================

print()
print("=" * 70)
print("API 1 - REQUISIÇÃO SAP")
print("=" * 70)

print("Endpoint:")
print(SAP_API_1_URL)

print()
print("Filtro:")
print(filtro)

print()
print("Select:")
print(select)


response = requests.get(
    SAP_API_1_URL,
    params=params,
    headers=headers,
    auth=(
        SAP_API_1_USUARIO,
        SAP_API_1_SENHA
    ),
    timeout=60
)


# ============================================================
# VALIDAÇÃO
# ============================================================

print()
print("=" * 70)
print("API 1 - RESPOSTA SAP")
print("=" * 70)

print(f"HTTP STATUS: {response.status_code}")

print()
print("URL FINAL ENVIADA:")
print(response.url)

if response.status_code != 200:
    print()
    print("CORPO DA RESPOSTA SAP:")
    print(response.text)

response.raise_for_status()


# ============================================================
# CONVERTER RETORNO
# ============================================================

dados = response.json()


if "d" in dados and "results" in dados["d"]:
    registros = dados["d"]["results"]

elif "value" in dados:
    registros = dados["value"]

else:
    raise ValueError(
        "Formato de retorno OData não reconhecido."
    )


df = pd.DataFrame(registros)


# ============================================================
# GARANTIR COLUNAS
# ============================================================

colunas = [
    "CompanyCode",
    "AccountingDocument",
    "FiscalYear",
    "AccountingDocumentItem",
    "DocumentReferenceID_2",
    "CompanyCodeCurrency",
    "AmountInCompanyCodeCurrency",
    "Dt_lcto",
    "NetDueDate",
    "Customer",
    "AssignmentReference",
    "HouseBank"
]


for coluna in colunas:
    if coluna not in df.columns:
        df[coluna] = None


df = df[colunas]


# ============================================================
# FORMATAR NET DUE DATE e DT_LCTO
# ============================================================

def formatar_data_sap(valor):

    if pd.isna(valor):
        return None

    try:

        # Formato SAP OData:
        # /Date(1753202604097)/

        timestamp = int(
            str(valor)
            .replace("/Date(", "")
            .replace(")/", "")
        )

        data = pd.to_datetime(
            timestamp,
            unit="ms"
        )

        return data.strftime("%d.%m.%Y")

    except Exception:

        return valor


df["Dt_lcto"] = df["Dt_lcto"].apply(
    formatar_data_sap
)

df["NetDueDate"] = df["NetDueDate"].apply(
    formatar_data_sap
)

# ============================================================
# REMOVER DUPLICIDADES DA API 1
# ============================================================

CHAVE_API_1 = [
    "CompanyCode",
    "AccountingDocument",
    "FiscalYear",
    "AccountingDocumentItem"
]


# ============================================================
# IDENTIFICAR DUPLICIDADES ANTES DA REMOÇÃO
# ============================================================

duplicados = df[
    df.duplicated(
        subset=CHAVE_API_1,
        keep=False
    )
].copy()


print()
print("=" * 70)
print("API 1 - VALIDAÇÃO DE DUPLICIDADES")
print("=" * 70)

print(
    f"Registros recebidos pela API 1: {len(df)}"
)

print(
    f"Registros envolvidos em duplicidade: "
    f"{len(duplicados)}"
)


if not duplicados.empty:

    print()
    print("Chaves duplicadas encontradas:")

    print(
        duplicados[
            CHAVE_API_1
        ]
        .drop_duplicates()
        .to_string(index=False)
    )


# ============================================================
# REMOVER DUPLICIDADES
# ============================================================

df = df.drop_duplicates(
    subset=CHAVE_API_1,
    keep="first"
).copy()

# ============================================================
# FILTRAR TÍTULOS JÁ PROCESSADOS
# ============================================================

titulos_processados, titulos_processados_set = (
    carregar_titulos_processados()
)

print()
print("=" * 70)
print("CONTROLE DE TÍTULOS JÁ PROCESSADOS")
print("=" * 70)

print(
    f"Títulos encontrados no CSV: "
    f"{len(titulos_processados_set)}"
)

if not df.empty:

    df["ChaveProcessamento"] = df.apply(
        montar_chave_titulo,
        axis=1
    )

    quantidade_antes = len(df)

    df = df[
        ~df["ChaveProcessamento"].isin(
            titulos_processados_set
        )
    ].copy()

    quantidade_depois = len(df)

    print(
        f"Títulos recebidos após deduplicação: "
        f"{quantidade_antes}"
    )

    print(
        f"Títulos já processados e ignorados: "
        f"{quantidade_antes - quantidade_depois}"
    )

    print(
        f"Títulos novos para processamento: "
        f"{quantidade_depois}"
    )

    df = df.drop(
        columns=["ChaveProcessamento"],
        errors="ignore"
    )

else:

    print("Nenhum título encontrado na API.")


# ============================================================
# RESULTADO API 1
# ============================================================

print()
print("=" * 70)
print("API 1 - RESULTADO APÓS DEDUPLICAÇÃO")
print("=" * 70)

print(f"Data utilizada: {data_d1_str}")

print(
    f"Registros após deduplicação: {len(df)}"
)


if df.empty:

    print()
    print("Nenhum registro encontrado.")

else:

    print()
    print(df.to_string(index=False))


# ============================================================
# VARIÁVEL FINAL API 1
# ============================================================

resultado_api_1 = df


# ============================================================
# ============================================================
# API 2 - DADOS CADASTRAIS DOS CLIENTES
# ============================================================
# ============================================================


# ============================================================
# CUSTOMERS DA API 1
# ============================================================

customers = (
    resultado_api_1["Customer"]
    .dropna()
    .astype(str)
    .str.strip()
    .unique()
    .tolist()
)


print()
print("=" * 70)
print("API 2 - CUSTOMERS ENCONTRADOS NA API 1")
print("=" * 70)

print(f"Clientes distintos: {len(customers)}")

if customers:
    print()
    print(customers)


# ============================================================
# SE NÃO HOUVER CLIENTES, NÃO CONSULTA API 2
# ============================================================

if not customers:

    print()
    print("Nenhum Customer encontrado na API 1.")
    print("API 2 não será consultada.")

    resultado_api_2_tax = pd.DataFrame()
    resultado_api_2_address = pd.DataFrame()

else:

    # ========================================================
    # FILTRO DOS CUSTOMERS
    # ========================================================

    filtro_customers = " or ".join(
        f"BusinessPartner eq '{customer}'"
        for customer in customers
    )


    # ========================================================
    # URL BASE API 2
    # ========================================================

    SAP_API_2_BASE = SAP_API_2_URL

    # ========================================================
    # ========================================================
    # API 2A - DOCUMENTO FISCAL
    # ========================================================
    # ========================================================

    url_tax = (
        f"{SAP_API_2_BASE}/A_BusinessPartnerTaxNumber"
    )


    params_tax = {

        "$filter": (
            f"({filtro_customers}) "
            "and (BPTaxType eq 'BR1' or BPTaxType eq 'BR2')"
        ),

        "$select": (
            "BusinessPartner,"
            "BPTaxType,"
            "BPTaxNumber"
        ),

        "$format": "json"
    }


    print()
    print("=" * 70)
    print("API 2A - DOCUMENTO FISCAL")
    print("=" * 70)

    print("Endpoint:")
    print(url_tax)

    print()
    print("Filtro:")
    print(params_tax["$filter"])

    print()
    print("Select:")
    print(params_tax["$select"])


    response_tax = requests.get(

        url_tax,

        params=params_tax,

        headers=headers,

        auth=(
            SAP_API_2_USUARIO,
            SAP_API_2_SENHA
        ),

        timeout=60
    )


    print()
    print("HTTP STATUS:")
    print(response_tax.status_code)

    print()
    print("URL FINAL ENVIADA:")
    print(response_tax.url)


    if response_tax.status_code != 200:

        print()
        print("CORPO DA RESPOSTA SAP:")
        print(response_tax.text)


    response_tax.raise_for_status()


    # ========================================================
    # CONVERTER RETORNO TAX
    # ========================================================

    dados_tax = response_tax.json()


    if (
        "d" in dados_tax
        and "results" in dados_tax["d"]
    ):

        registros_tax = dados_tax["d"]["results"]

    elif "value" in dados_tax:

        registros_tax = dados_tax["value"]

    else:

        raise ValueError(
            "Formato de retorno TaxNumber não reconhecido."
        )


    resultado_api_2_tax = pd.DataFrame(
        registros_tax
    )


    print()
    print(
        f"Registros fiscais encontrados: "
        f"{len(resultado_api_2_tax)}"
    )


    # ========================================================
    # GARANTIR COLUNAS TAX
    # ========================================================

    colunas_tax = [
        "BusinessPartner",
        "BPTaxType",
        "BPTaxNumber"
    ]


    for coluna in colunas_tax:

        if coluna not in resultado_api_2_tax.columns:

            resultado_api_2_tax[coluna] = None


    resultado_api_2_tax = resultado_api_2_tax[
        colunas_tax
    ]

    # ========================================================
    # ========================================================
    # API 2C - NOME DO CLIENTE
    # ========================================================
    # ========================================================

    url_business_partner = (
        f"{SAP_API_2_BASE}/A_BusinessPartner"
    )


    params_business_partner = {

        "$filter": filtro_customers,

        "$select": (
            "BusinessPartner,"
            "BusinessPartnerFullName"
        ),

        "$format": "json"
    }


    print()
    print("=" * 70)
    print("API 2C - NOME DO CLIENTE")
    print("=" * 70)

    print("Endpoint:")
    print(url_business_partner)

    print()
    print("Filtro:")
    print(params_business_partner["$filter"])

    print()
    print("Select:")
    print(params_business_partner["$select"])


    response_business_partner = requests.get(

        url_business_partner,

        params=params_business_partner,

        headers=headers,

        auth=(
            SAP_API_2_USUARIO,
            SAP_API_2_SENHA
        ),

        timeout=60
    )


    print()
    print("HTTP STATUS:")
    print(response_business_partner.status_code)

    print()
    print("URL FINAL ENVIADA:")
    print(response_business_partner.url)


    if response_business_partner.status_code != 200:

        print()
        print("CORPO DA RESPOSTA SAP:")
        print(response_business_partner.text)


    response_business_partner.raise_for_status()


    # ========================================================
    # CONVERTER RETORNO BUSINESS PARTNER
    # ========================================================

    dados_business_partner = (
        response_business_partner.json()
    )


    if (
        "d" in dados_business_partner
        and "results" in dados_business_partner["d"]
    ):

        registros_business_partner = (
            dados_business_partner["d"]["results"]
        )

    elif "value" in dados_business_partner:

        registros_business_partner = (
            dados_business_partner["value"]
        )

    else:

        raise ValueError(
            "Formato de retorno BusinessPartner não reconhecido."
        )


    resultado_api_2_business_partner = pd.DataFrame(
        registros_business_partner
    )


    print()
    print(
        f"Clientes encontrados: "
        f"{len(resultado_api_2_business_partner)}"
    )


    # ========================================================
    # GARANTIR COLUNAS BUSINESS PARTNER
    # ========================================================

    colunas_business_partner = [
        "BusinessPartner",
        "BusinessPartnerFullName"
    ]


    for coluna in colunas_business_partner:

        if coluna not in resultado_api_2_business_partner.columns:

            resultado_api_2_business_partner[coluna] = None


    resultado_api_2_business_partner = (
        resultado_api_2_business_partner[
            colunas_business_partner
        ]
    )

    # ========================================================
    # ========================================================
    # API 2B - ENDEREÇO + E-MAIL
    # ========================================================
    # ========================================================

    url_address = (
        f"{SAP_API_2_BASE}/A_BusinessPartnerAddress"
    )


    params_address = {

        "$filter": filtro_customers,

        "$select": (
            "BusinessPartner,"
            "AddressID,"
            "CityName,"
            "District,"
            "HouseNumber,"
            "PostalCode,"
            "Region,"
            "StreetName,"
            "to_EmailAddress"
        ),

        "$format": "json"
    }


    print()
    print("=" * 70)
    print("API 2B - ENDEREÇO + E-MAIL")
    print("=" * 70)

    print("Endpoint:")
    print(url_address)

    print()
    print("Filtro:")
    print(params_address["$filter"])

    print()
    print("Select:")
    print(params_address["$select"])


    response_address = requests.get(

        url_address,

        params=params_address,

        headers=headers,

        auth=(
            SAP_API_2_USUARIO,
            SAP_API_2_SENHA
        ),

        timeout=60
    )


    print()
    print("HTTP STATUS:")
    print(response_address.status_code)

    print()
    print("URL FINAL ENVIADA:")
    print(response_address.url)


    if response_address.status_code != 200:

        print()
        print("CORPO DA RESPOSTA SAP:")
        print(response_address.text)


    response_address.raise_for_status()


    # ========================================================
    # CONVERTER RETORNO ADDRESS
    # ========================================================

    dados_address = response_address.json()


    if (
        "d" in dados_address
        and "results" in dados_address["d"]
    ):

        registros_address = (
            dados_address["d"]["results"]
        )

    elif "value" in dados_address:

        registros_address = dados_address["value"]

    else:

        raise ValueError(
            "Formato de retorno Address não reconhecido."
        )


    resultado_api_2_address = pd.DataFrame(
        registros_address
    )


    print()
    print(
        f"Endereços encontrados: "
        f"{len(resultado_api_2_address)}"
    )


    # ========================================================
    # EXPANDIR E-MAIL
    # ========================================================

    def extrair_email(valor):

        if not isinstance(valor, list):

            return None

        if len(valor) == 0:

            return None

        primeiro = valor[0]

        if isinstance(primeiro, dict):

            return primeiro.get("EmailAddress")

        return None


    if "to_EmailAddress" in resultado_api_2_address.columns:

        resultado_api_2_address["EmailAddress"] = (
            resultado_api_2_address[
                "to_EmailAddress"
            ].apply(extrair_email)
        )

    else:

        resultado_api_2_address["EmailAddress"] = None


    # ========================================================
    # GARANTIR COLUNAS ADDRESS
    # ========================================================

    colunas_address = [
        "BusinessPartner",
        "AddressID",
        "CityName",
        "District",
        "HouseNumber",
        "PostalCode",
        "Region",
        "StreetName",
        "EmailAddress"
    ]


    for coluna in colunas_address:

        if coluna not in resultado_api_2_address.columns:

            resultado_api_2_address[coluna] = None


    resultado_api_2_address = (
        resultado_api_2_address[
            colunas_address
        ]
    )


# ============================================================
# ============================================================
# MERGE API 1 + API 2A + API 2B + API 2C
# ============================================================
# ============================================================

if not resultado_api_1.empty:

    # ========================================================
    # MERGE DOCUMENTO FISCAL - API 2A
    # ========================================================

    df_final = resultado_api_1.merge(

        resultado_api_2_tax[
            [
                "BusinessPartner",
                "BPTaxType",
                "BPTaxNumber"
            ]
        ],

        left_on="Customer",

        right_on="BusinessPartner",

        how="left"
    )


    # ========================================================
    # REMOVER CHAVE DUPLICADA
    # ========================================================

    df_final = df_final.drop(
        columns=["BusinessPartner"],
        errors="ignore"
    )


    # ========================================================
    # MERGE ENDEREÇO + E-MAIL - API 2B
    # ========================================================

    df_final = df_final.merge(

        resultado_api_2_address[
            [
                "BusinessPartner",
                "AddressID",
                "CityName",
                "District",
                "HouseNumber",
                "PostalCode",
                "Region",
                "StreetName",
                "EmailAddress"
            ]
        ],

        left_on="Customer",

        right_on="BusinessPartner",

        how="left"
    )


    # ========================================================
    # REMOVER CHAVE DUPLICADA
    # ========================================================

    df_final = df_final.drop(
        columns=["BusinessPartner"],
        errors="ignore"
    )


    # ========================================================
    # MERGE NOME DO CLIENTE - API 2C
    # ========================================================

    df_final = df_final.merge(

        resultado_api_2_business_partner[
            [
                "BusinessPartner",
                "BusinessPartnerFullName"
            ]
        ],

        left_on="Customer",

        right_on="BusinessPartner",

        how="left"
    )


    # ========================================================
    # REMOVER CHAVE DUPLICADA
    # ========================================================

    df_final = df_final.drop(
        columns=["BusinessPartner"],
        errors="ignore"
    )


else:

    df_final = resultado_api_1.copy()


# ============================================================
# ============================================================
# RESULTADO FINAL
# ============================================================
# ============================================================

print()
print("=" * 70)
print("RESULTADO FINAL - API 1 + API 2A + API 2B + API 2C")
print("=" * 70)

print(
    f"Registros finais: {len(df_final)}"
)


print()

if df_final.empty:

    print("Nenhum registro para processamento.")

else:

    print(
        df_final.to_string(index=False)
    )


# ============================================================
# VARIÁVEL FINAL
# ============================================================

resultado_final = df_final

# ============================================================
# FUNÇÃO AUXILIARES
# ============================================================
def limpar_numeros(valor):
    return "".join(
        c for c in str(valor)
        if c.isdigit()
    )

def formatar_data_cnab(valor):

    if pd.isna(valor):
        return "00000000"

    valor = str(valor).strip()

    for formato in [
        "%d.%m.%Y",
        "%d/%m/%Y",
        "%Y-%m-%d",
        "%d%m%Y"
    ]:

        try:
            return pd.to_datetime(
                valor,
                format=formato
            ).strftime("%d%m%Y")

        except:
            pass

    return "00000000"

def formatar_data_cnab_mais_dias(valor, dias=1):
    """
    Mesma lógica de formatar_data_cnab, mas soma 'dias' à data antes
    de formatar. Usado para a Data do Juros de Mora, que o banco
    exige que seja MAIOR que a Data de Vencimento (nunca igual).
    """

    if pd.isna(valor):
        return "00000000"

    valor = str(valor).strip()

    for formato in [
        "%d.%m.%Y",
        "%d/%m/%Y",
        "%Y-%m-%d",
        "%d%m%Y"
    ]:

        try:
            data = pd.to_datetime(
                valor,
                format=formato
            ) + pd.Timedelta(days=dias)

            return data.strftime("%d%m%Y")

        except:
            pass

    return "00000000"

def formatar_valor_cnab(valor, tamanho=15):

    valor = str(valor).replace(",", ".").strip()

    try:
        valor = float(valor)
    except:
        valor = 0

    centavos = round(valor * 100)

    return str(centavos).zfill(tamanho)

def calcular_juros_diarios(valor, taxa_mensal=0.025):

    valor = str(valor).replace(",", ".").strip()

    try:
        valor = float(valor)
    except:
        valor = 0

    juros_dia = valor * taxa_mensal / 30

    centavos = round(juros_dia * 100)

    return str(centavos).zfill(15)

# NÃO USAR MAIS: o Nosso Número não pode ser derivado do documento
# SAP, pois precisa respeitar a faixa liberada pelo Sofisa no
# convênio (ver obter_proximo_nosso_numero). Mantida apenas para
# referência histórica.
def montar_seu_numero(row):

    base = str(
        row["DocumentReferenceID_2"]
    ).split("-")[0]

    base = limpar_numeros(base)

    item = str(
        row["AccountingDocumentItem"]
    ).zfill(3)

    nosso_numero = (
        base + item
    )[-9:]

    return nosso_numero.zfill(10)[-10:]

#def montar_seu_numero(row):

    valor = str(
        row["DocumentReferenceID_2"]
    ).split("-")[0]

    valor = limpar_numeros(valor)

    return valor.zfill(10)[-10:]

def montar_referencia_empresa(row):

    referencia = (
        str(row["Customer"]).strip()
        + str(row["AccountingDocument"]).strip()
        + str(row["FiscalYear"]).strip()
    )

    return referencia.zfill(25)

def remover_acentos(valor):
    if valor is None:
        return ""

    texto = str(valor)

    texto = unicodedata.normalize(
        "NFKD",
        texto
    )

    texto = "".join(
        caractere
        for caractere in texto
        if not unicodedata.combining(caractere)
    )

    return texto.upper()

def enviar_arquivos_slack(arquivos, mensagem=None):

    # ========================================================
    # HEADERS
    # ========================================================

    headers_slack = {
        "Authorization": f"Bearer {SLACK_TOKEN}"
    }


    # ========================================================
    # VALIDAR
    # ========================================================

    if not arquivos:

        raise ValueError(
            "Nenhum arquivo foi informado para envio ao Slack."
        )


    # ========================================================
    # LISTA DOS ARQUIVOS
    # ========================================================

    arquivos_upload = []


    # ========================================================
    # UPLOAD DE CADA ARQUIVO
    # ========================================================

    for arquivo in arquivos:

        nome_arquivo = arquivo["nome"]

        conteudo_bytes = arquivo["conteudo"]

        tamanho_arquivo = len(
            conteudo_bytes
        )


        print()
        print("=" * 70)
        print("SLACK - PREPARANDO ARQUIVO")
        print("=" * 70)

        print(
            f"Arquivo: {nome_arquivo}"
        )

        print(
            f"Tamanho: {tamanho_arquivo} bytes"
        )


        # ====================================================
        # ETAPA 1
        # OBTER URL DE UPLOAD
        # ====================================================

        url_get_upload = (
            "https://slack.com/api/files.getUploadURLExternal"
        )


        params_upload = {

            "filename":
                nome_arquivo,

            "length":
                tamanho_arquivo
        }


        response = requests.get(

            url_get_upload,

            headers=headers_slack,

            params=params_upload,

            timeout=60
        )


        print()
        print(
            "GET UPLOAD URL"
        )

        print(
            f"HTTP STATUS: {response.status_code}"
        )


        response.raise_for_status()


        resultado = response.json()


        if not resultado.get("ok"):

            raise RuntimeError(
                "Erro ao obter URL de upload do Slack: "
                f"{resultado}"
            )


        upload_url = resultado["upload_url"]

        file_id = resultado["file_id"]


        print(
            f"File ID: {file_id}"
        )


        # ====================================================
        # ETAPA 2
        # ENVIAR ARQUIVO DIRETAMENTE DA MEMÓRIA
        # ====================================================

        arquivo_memoria = BytesIO(
            conteudo_bytes
        )


        response_upload = requests.post(

            upload_url,

            files={
                "file": (
                    nome_arquivo,
                    arquivo_memoria,
                    arquivo.get("mime", "text/plain")
                )
            },

            timeout=60
        )


        print()
        print(
            "UPLOAD STATUS:",
            response_upload.status_code
        )


        response_upload.raise_for_status()


        # ====================================================
        # GUARDAR FILE ID
        # ====================================================

        arquivos_upload.append({

            "id":
                file_id,

            "title":
                nome_arquivo
        })


    # ========================================================
    # ETAPA 3
    # FINALIZAR TODOS OS UPLOADS
    # ========================================================

    print()
    print("=" * 70)
    print("SLACK - PUBLICANDO ARQUIVOS")
    print("=" * 70)


    url_complete = (
        "https://slack.com/api/files.completeUploadExternal"
    )


    data_complete = {

        "files":
            json.dumps(arquivos_upload),

        "channel_id":
            SLACK_CANAL
    }


    if mensagem:

        data_complete["initial_comment"] = (
            mensagem
        )


    # ========================================================
    # PUBLICAR
    # ========================================================

    response_complete = requests.post(

        url_complete,

        headers=headers_slack,

        data=data_complete,

        timeout=60
    )


    print()
    print(
        "COMPLETE STATUS:",
        response_complete.status_code
    )


    print(
        "RESPOSTA:",
        response_complete.text
    )


    response_complete.raise_for_status()


    resultado_final_slack = (
        response_complete.json()
    )


    if not resultado_final_slack.get("ok"):

        raise RuntimeError(
            "Erro ao publicar arquivos no Slack: "
            f"{resultado_final_slack}"
        )


    print()
    print("=" * 70)
    print("ARQUIVOS ENVIADOS PARA O SLACK")
    print("=" * 70)


    for arquivo in arquivos:

        print(
            f"✓ {arquivo['nome']}"
        )


    return resultado_final_slack

def enviar_mensagem_slack(mensagem):

    headers_slack = {
        "Authorization": f"Bearer {SLACK_TOKEN}",
        "Content-Type": "application/json"
    }

    payload = {
        "channel": SLACK_CANAL,
        "text": mensagem
    }

    response = requests.post(
        "https://slack.com/api/chat.postMessage",
        headers=headers_slack,
        json=payload,
        timeout=60
    )

    print()
    print("=" * 70)
    print("SLACK - ENVIO DE MENSAGEM")
    print("=" * 70)

    print(
        f"HTTP STATUS: {response.status_code}"
    )

    print(
        f"RESPOSTA: {response.text}"
    )

    response.raise_for_status()

    resultado = response.json()

    if not resultado.get("ok"):
        raise RuntimeError(
            "Erro ao enviar mensagem para o Slack: "
            f"{resultado}"
        )

    print("✓ Mensagem enviada para o Slack.")

    return resultado

# ============================================================
# FUNÇÃO PRINCIPAL
# ============================================================

def gerar_header(company_code, sequencia_arquivo=1):

    # ========================================================
    # BUSCAR EMPRESA
    # ========================================================

    empresa = DEPARA_COMPANY_CODE.get(company_code)

    if empresa is None:

        raise ValueError(
            f"CompanyCode não parametrizado: {company_code}"
        )

    
    # ========================================================
    # DADOS DA EMPRESA
    # ========================================================

    cnpj = str(
        empresa["CNPJ"]
    ).replace(".", "").replace("/", "").replace("-", "").strip()


    agencia = str(
        empresa["ag"]
    ).strip()


    conta = str(
        empresa["cc"]
    ).strip()


    nome_empresa = str(
        empresa["Nome"]
    ).strip()

    convenio = str(
        empresa["convenio"]
    ).strip().ljust(20)

    nsa = str(
        obter_proximo_nsa(company_code)
    ).zfill(6)

    # ========================================================
    # DATA / HORA
    # ========================================================

    agora = datetime.now()

    data_geracao = agora.strftime("%d%m%Y")

    hora_geracao = agora.strftime("%H%M%S")


    # ========================================================
    # MONTAGEM DOS CAMPOS
    # ========================================================

    header = (

        # 01.0 - Código do Banco
        CODIGO_BANCO

        # 04.0 - CNAB
        + "00000"
        + " " * 9

        # 05.0 - Tipo Inscrição
        + TIPO_INSCRICAO

        # 06.0 - Número Inscrição
        + cnpj

        # 07.0 - Convênio
        + convenio

        # 08.0 - Agência
        + agencia

        # 10.0 - Número da Conta + 11.0 DV
        + conta

        # 13.0 - Nome da Empresa
        + nome_empresa[:30].ljust(30)

        # 14.0 - Nome do Banco
        + NOME_BANCO[:30].rjust(30)

        # 15.0 - CNAB
        + " " * 10

        # 16.0 - Código do Arquivo
        + CODIGO_ARQUIVO

        # 17.0 - Data de Geração
        + data_geracao

        # 18.0 - Hora de Geração
        + hora_geracao

        # 19.0 - NSA
        + nsa

        # 20.0 - Layout
        + LAYOUT_ARQUIVO

        # 21.0 - Densidade
        + "01600"

        # 22.0 - Reservado Banco
        + " " * 20

        # 23.0 - Reservado Empresa
        + " " * 20

        # 24.0 - CNAB - uso exclusivo FEBRABAN/CNAB
        # Colunas 226 a 228 (posições 15 a 17 deste bloco de 29)
        # devem ser preenchidas com zeros; o restante permanece em branco.
        + " " * 14 + "000" + " " * 12
    )

    return header

def gerar_header_lote(company_code, sequencia_arquivo=1):

    # ========================================================
    # BUSCAR EMPRESA
    # ========================================================

    empresa = DEPARA_COMPANY_CODE.get(company_code)

    if empresa is None:

        raise ValueError(
            f"CompanyCode não parametrizado: {company_code}"
        )


    # ========================================================
    # DADOS DA EMPRESA
    # ========================================================

    cnpj = str(
        empresa["CNPJ"]
    ).replace(".", "").replace("/", "").replace("-", "").strip()


    agencia = str(
        empresa["ag"]
    ).strip()


    conta = str(
        empresa["cc"]
    ).strip()


    nome_empresa = str(
        empresa["Nome"]
    ).strip()

    convenio = str(
        empresa["convenio"]
    ).strip().ljust(20)


    # ========================================================
    # DATA / HORA
    # ========================================================

    agora = date.today()

    data_geracao = agora.strftime("%d%m%Y")


    # ========================================================
    # MONTAGEM DOS CAMPOS
    # ========================================================

    headerlote = (

        # 01.0 - Código do Banco
        CODIGO_BANCO
        +"0001"
        
        # 04.0 - CNAB
        + "1R01  030 "

        # 05.0 - Tipo Inscrição
        + TIPO_INSCRICAO
        + "0"

        # 06.0 - Número Inscrição
        + cnpj

        # 07.0 - Convênio
        + convenio

        # 08.0 - Agência
        + agencia

        # 10.0 - Número da Conta + 11.0 DV
        + conta

        # 13.0 - Nome da Empresa
        + nome_empresa[:30].ljust(30)

        # 15.0 - CNAB
        + " " * 80

        # 20.1 - CNAB
        + "00000001"
        
        # 21.1 - Data de Geração
        + data_geracao
        
        # 22.1 - CNAB
        + "00000000"
        
        # 23.1 - CNAB
        + " " * 33
    )

    return headerlote

def gerar_segmentoP(company_code, row, contador):

    empresa = DEPARA_COMPANY_CODE[company_code]

    agencia = str(
        empresa["ag"]
    ).strip()

    conta = str(
        empresa["cc"]
    ).strip()

    # O Nosso Número precisa vir de uma faixa liberada pelo Sofisa
    # para o convênio (não pode ser derivado do documento SAP).
    nosso_numero = obter_proximo_nosso_numero(company_code)

    # Carteira usada no cálculo do DV — precisa ser a MESMA que está
    # nas colunas 44-46 do literal abaixo ("50000121" termina em "121").
    carteira = "121"

    # Conferido com um arquivo aceito pelo banco: a fórmula usa a
    # agência SEM os zeros à esquerda (ex.: "78", não "00078"),
    # mesmo que no arquivo ela apareça com zeros de preenchimento.
    agencia_dv = 7

    dv_nosso_numero = calcular_dv_nosso_numero(
        agencia_dv,
        carteira,
        nosso_numero
    )

    seu_numero = montar_seu_numero(row)

    vencimento = formatar_data_cnab(
        row["NetDueDate"]
    )

    # A Data do Juros de Mora tem que ser MAIOR que o vencimento.
    data_juros_mora = formatar_data_cnab_mais_dias(
        row["NetDueDate"],
        dias=1
    )

    data_emissao = formatar_data_cnab(
        row["Dt_lcto"]
    )

    valor = formatar_valor_cnab(
        row["AmountInCompanyCodeCurrency"]
    )

    juros_diarios = calcular_juros_diarios(
        row["AmountInCompanyCodeCurrency"]
    )

    referencia_empresa = montar_referencia_empresa(
        row
    )

    segmentoP = (

        CODIGO_BANCO

        + "0001"

        + "3"

        + str(contador).zfill(5)

        + "P"

        + " "

        + "01"

        + agencia

        + conta

        # Literal de carteira/identificação ajustado conforme crítica
        # do banco: colunas 39-41 devem ser "000" e colunas 44-46
        # devem ser "121". CONFIRMAR com o manual Sofisa o significado
        # exato de cada posição antes de subir em produção.
        + "501450903"

        + nosso_numero
        + dv_nosso_numero
        + "1"
        + "1"
        + "2"
        + "1"
        + "2"

        + seu_numero

        + " " * 5

        + vencimento

        + valor

        + "0"
        + agencia

        + "02"

        + "N"

        + data_emissao

        + "2"

        + data_juros_mora

        + "0"*12 + "250"

        + "0"

        + "00000000"

        + "0" * 15

        + "0" * 15

        + "0" * 15

        + referencia_empresa

        + "1"

        + "09"

        + "2"

        + "000"

        + "09"

        + "0000000000"

        + " "
    )

    if len(segmentoP) != 240:
        raise ValueError(
            f"Segmento P possui {len(segmentoP)} caracteres"
        )

    return segmentoP

def gerar_segmentoQ(company_code, row, contador):

    tpdoc = (
        "2"
        if str(row["BPTaxType"]).strip().upper() == "BR1"
        else "1"
    )

    documento = limpar_numeros(
        row["BPTaxNumber"]
    ).zfill(15)

    nome = str(
        row["BusinessPartnerFullName"]
    ).strip()[:40].ljust(40)

    endereco = (
        str(row["StreetName"]).strip()
        + " "
        + str(row["HouseNumber"]).strip()
    )

    endereco = endereco[:40].ljust(40)

    bairro = str(
        row["District"]
    ).strip()[:15].ljust(15)

    cep = limpar_numeros(
        row["PostalCode"]
    ).zfill(8)

    cidade = str(
        row["CityName"]
    ).strip()[:15].ljust(15)

    uf = str(
        row["Region"]
    ).strip().upper()[:2].ljust(2)

    segmentoQ = (

        CODIGO_BANCO

        + "0001"

        + "3"

        + str(contador).zfill(5)

        + "Q"

        + " "

        + "01"

        + tpdoc

        + documento

        + nome

        + endereco

        + bairro

        + cep[:5]

        + cep[5:]

        + cidade

        + uf

        + "0"

        + "0" * 15

        + " " * 40

        + "000"

        + " " * 20

        + " " * 8
    )

    if len(segmentoQ) != 240:
        raise ValueError(
            f"Segmento Q possui {len(segmentoQ)} caracteres"
        )

    return segmentoQ

def gerar_trailer_lote(quantidade_linhas, quantidade_titulos, valor_total):

    # ========================================================
    # MONTAGEM DOS CAMPOS
    # ========================================================

    trailerlote = (
        CODIGO_BANCO
        + "0001"
        + "5"
        + " " * 9
        + str(quantidade_linhas).zfill(6)
        + str(quantidade_titulos).zfill(6)
        + str(valor_total).zfill(17)
        + "0" * 6
        + "0" * 17
        + "0" * 6
        + "0" * 17
        + "0" * 6
        + "0" * 17
        + " " * 125
    )

    if len(trailerlote) != 240:
        raise ValueError(
            f"Trailer lote possui {len(trailerlote)} caracteres"
        )

    return trailerlote

def gerar_trailer(quantidade_lotes, quantidade_linhas):

    trailer = (
        CODIGO_BANCO
        + "9999"
        + "9"
        + " " * 9
        + str(quantidade_lotes).zfill(6)
        + str(quantidade_linhas).zfill(6)
        + "000000"
        + " " * 205
    )

    if len(trailer) != 240:
        raise ValueError(
            f"Trailer arquivo possui {len(trailer)} caracteres"
        )

    return trailer

def gerar_cnab(company_code, df_empresa):

    linhas = []

    # ========================================================
    # HEADER ARQUIVO
    # ========================================================

    linhas.append(
        gerar_header(company_code)
    )

    # ========================================================
    # HEADER LOTE
    # ========================================================

    linhas.append(
        gerar_header_lote(company_code)
    )

    # ========================================================
    # SEGMENTOS
    # ========================================================

    contador = 1

    quantidade_titulos = 0
    valor_total = 0

    for _, row in df_empresa.iterrows():

        segmento_p = gerar_segmentoP(
            company_code,
            row,
            contador
        )

        linhas.append(segmento_p)

        contador += 1

        segmento_q = gerar_segmentoQ(
            company_code,
            row,
            contador
        )

        linhas.append(segmento_q)

        contador += 1

        quantidade_titulos += 1

        valor_total += round(
            float(
                str(
                    row["AmountInCompanyCodeCurrency"]
                ).replace(",", ".")
            ) * 100
        )

    # ========================================================
    # TRAILER LOTE
    # ========================================================

    quantidade_linhas_lote = (
        1                       # header lote
        + (quantidade_titulos * 2)
        + 1                    # trailer lote
    )

    linhas.append(
        gerar_trailer_lote(
            quantidade_linhas_lote,
            quantidade_titulos,
            valor_total
        )
    )

    # ========================================================
    # TRAILER ARQUIVO
    # ========================================================

    quantidade_linhas_total = len(linhas) + 1

    linhas.append(
        gerar_trailer(
            1,
            quantidade_linhas_total
        )
    )


    # ========================================================
    # ARQUIVO
    # ========================================================

    hoje = datetime.now().strftime("%d%m%y%H%M")

    nome_arquivo = (
        f"{company_code}_{hoje}.txt"
    )

    conteudo_arquivo = ""

    for linha in linhas:

        linha_sem_acentos = remover_acentos(
            linha
        )

        conteudo_arquivo += (
            linha_sem_acentos + "\n"
        )

    conteudo_bytes = conteudo_arquivo.encode("ascii")

    print()
    print("=" * 70)
    print("CNAB GERADO")
    print("=" * 70)
    print(f"Empresa: {company_code}")
    print(f"Arquivo: {nome_arquivo}")
    print(f"Títulos: {quantidade_titulos}")
    print(f"Linhas: {len(linhas)}")
    print(
        f"Valor: R$ {valor_total / 100:,.2f}"
    )

    return {
        "nome": nome_arquivo,
        "conteudo": conteudo_bytes,
        "valor_total": valor_total 
    }


# ============================================================
# XML DAS NOTAS (PIPEFY) — vão zipados junto com o CNAB no Slack
# ============================================================
#
# CHAVE DO MÓDULO: mude para False se o consumo de API do Pipefy estiver alto.
# Desligado, o script gera e envia só o CNAB, exatamente como antes.
BUSCAR_XML_PIPEFY = True

PIPEFY_API_URL = "https://api.pipefy.com/graphql"
PIPEFY_TOKEN = parametros.get("PIPEFY_TOKEN", "")
PIPE_ID_NF = os.getenv("PIPEFY_PIPE_ID_NF", "")
CAMPOS_NUMERO_NF = ["n_mero_da_nota_fiscal", "copy_of_n_mero_da_nota_fiscal"]
CAMPO_XML = "xml_da_nota"
NS_NFE = "http://www.portalfiscal.inf.br/nfe"

_QUERY_FIND_CARDS = """
query Buscar($pipe: ID!, $campo: String!, $valor: String!, $after: String) {
  findCards(pipeId: $pipe, search: {fieldId: $campo, fieldValue: $valor}, first: 50, after: $after) {
    pageInfo { hasNextPage endCursor }
    edges {
      node {
        id
        attachments { url path }
        fields { field { id } value array_value }
      }
    }
  }
}
"""

# contador de chamadas GraphQL ao Pipefy (vai no Slack para acompanhar o consumo)
_PIPEFY_CHAMADAS = {"n": 0}


def _headers_pipefy():
    return {
        "Authorization": f"Bearer {PIPEFY_TOKEN}",
        "Content-Type": "application/json"
    }


def numero_nf(document_reference):
    """'00002534-1' -> '2534' ; '001234567-8' -> '40788'."""
    base = str(document_reference or "").split("-", 1)[0].strip()
    base = re.sub(r"\D", "", base)
    return base.lstrip("0") or ("0" if base else "")


def numero_nf_com_digito(document_reference):
    """'000123-4' -> '1234' (todos os dígitos da referência, sem zeros à esquerda)."""
    return re.sub(r"\D", "", str(document_reference or "")).lstrip("0")


def buscar_cards_nf(nf, nf_com_digito=None):
    """Busca nos 2 campos de nº da NF e PARA NA PRIMEIRA COMBINAÇÃO QUE ACHA card.
    Ordem ('000123-4'):  '123' nos dois campos  ->  '1234' nos dois campos.
    Sem versão com zeros à esquerda."""
    for valor in dict.fromkeys(v for v in (nf, nf_com_digito) if v):
        for campo in CAMPOS_NUMERO_NF:
            encontrados = {}
            after = None
            while True:
                _PIPEFY_CHAMADAS["n"] += 1
                r = requests.post(
                    PIPEFY_API_URL,
                    headers=_headers_pipefy(),
                    timeout=30,
                    json={
                        "query": _QUERY_FIND_CARDS,
                        "variables": {
                            "pipe": PIPE_ID_NF,
                            "campo": campo,
                            "valor": valor,
                            "after": after
                        }
                    }
                )
                r.raise_for_status()
                dados = r.json()
                if "errors" in dados:
                    raise RuntimeError(f"Pipefy findCards ({campo}={valor}): {dados['errors']}")
                conn = dados["data"]["findCards"]
                for e in conn.get("edges", []):
                    encontrados[e["node"]["id"]] = e["node"]
                if not conn["pageInfo"]["hasNextPage"]:
                    break
                after = conn["pageInfo"]["endCursor"]
            if encontrados:
                return list(encontrados.values())
    return []


def _caminho_pipefy(v):
    v = re.sub(r"^https?://[^/]+/", "", str(v).split("?", 1)[0])
    return v.lstrip("/")


def urls_xml_card(card):
    """Links baixáveis do XML: o campo xml_da_nota guarda o CAMINHO; o link
    assinado vem em card.attachments (casa os dois pelo caminho)."""
    valores = []
    for f in card.get("fields", []):
        if f["field"]["id"] != CAMPO_XML:
            continue
        valores += [str(v) for v in (f.get("array_value") or [])]
        bruto = str(f.get("value") or "")
        if bruto and not valores:
            valores += re.findall(r'[^\s"\[\],]+\.(?:xml|zip)[^\s"\[\],]*', bruto, re.I) or [bruto]
    valores = [v.strip() for v in valores if v.strip()]

    anexos = card.get("attachments") or []
    urls = []
    for valor in valores:
        alvo = _caminho_pipefy(valor)
        nome = alvo.rsplit("/", 1)[-1]
        casados = [
            a["url"] for a in anexos
            if a.get("url") and (
                _caminho_pipefy(a.get("path") or "") == alvo
                or _caminho_pipefy(a.get("url") or "") == alvo
                or (nome and (a.get("path") or "").endswith(nome))
            )
        ]
        if casados:
            urls += casados
        elif valor.startswith("http"):
            urls.append(valor)
        else:
            urls.append("https://app.pipefy.com/" + valor.lstrip("/"))
    if not urls:
        urls = [
            a["url"] for a in anexos
            if a.get("url") and re.search(r"\.(xml|zip)(\?|$)", (a.get("path") or a["url"]), re.I)
        ]
    return list(dict.fromkeys(urls))


def _extrair_xml(conteudo):
    """Aceita XML puro (com ou sem BOM) ou ZIP com um .xml dentro."""
    bruto = conteudo.lstrip(b"\xef\xbb\xbf").lstrip()
    if bruto.startswith(b"<"):
        return bruto
    if bruto.startswith(b"PK"):
        with zipfile.ZipFile(BytesIO(conteudo)) as z:
            for nome in z.namelist():
                if nome.lower().endswith(".xml"):
                    return z.read(nome).lstrip(b"\xef\xbb\xbf").lstrip()
    return b""


def baixar_xml_pipefy(url):
    """Link pré-assinado primeiro (não é chamada GraphQL); se falhar, com o token."""
    for headers in (None, {"Authorization": f"Bearer {PIPEFY_TOKEN}"}):
        try:
            r = requests.get(url, headers=headers, timeout=60)
            if r.ok:
                xml = _extrair_xml(r.content)
                if xml:
                    return xml
        except requests.RequestException:
            pass
    return b""


def _ler_nfe_basico(conteudo):
    ns = {"n": NS_NFE}
    root = ET.fromstring(conteudo)
    inf = root.find(".//n:infNFe", ns)
    if inf is None:
        raise ValueError("XML não é uma NF-e")

    def texto(caminho):
        el = inf.find(caminho, ns)
        return (el.text or "").strip() if el is not None else ""

    return {
        "nNF": texto("n:ide/n:nNF"),
        "cnpj_emit": re.sub(r"\D", "", texto("n:emit/n:CNPJ")),
        "doc_dest": re.sub(r"\D", "", texto("n:dest/n:CNPJ") or texto("n:dest/n:CPF")),
    }


def xml_da_nota(company_code, nf, doc_cliente, referencia=None):
    """Busca o XML da NF no Pipefy e confere: número da NF, destinatário = cliente do
    título (BPTaxNumber) e emitente = mesma raiz de CNPJ da empresa. Devolve
    (bytes_do_xml, None) ou (None, motivo)."""
    cards = buscar_cards_nf(nf, numero_nf_com_digito(referencia))
    if not cards:
        return None, "não encontrada no pipe"
    raiz_empresa = re.sub(r"\D", "", DEPARA_COMPANY_CODE.get(company_code, {}).get("CNPJ", ""))[:8]
    motivo = "card sem XML"
    for card in cards:
        for url in urls_xml_card(card):
            conteudo = baixar_xml_pipefy(url)
            if not conteudo:
                motivo = "XML não baixou"
                continue
            try:
                nfe = _ler_nfe_basico(conteudo)
            except Exception:
                motivo = "arquivo não é uma NF-e válida"
                continue
            if nfe["nNF"].lstrip("0") != nf:
                motivo = f"XML é da NF {nfe['nNF']}"
                continue
            if doc_cliente and nfe["doc_dest"] != doc_cliente:
                motivo = "destinatário do XML não é o cliente do título"
                continue
            if raiz_empresa and nfe["cnpj_emit"][:8] != raiz_empresa:
                motivo = "XML emitido por outra empresa"
                continue
            return conteudo, None
    return None, motivo


def montar_zip_xmls(df_titulos):
    """Uma NF por referência distinta (várias parcelas da mesma NF = 1 XML).
    Zip com uma pasta por empresa: BR01/NF_41477.xml, BR04/NF_8894.xml ...
    Devolve (arquivo_para_slack ou None, resumo)."""
    _PIPEFY_CHAMADAS["n"] = 0
    notas = {}
    for _, row in df_titulos.iterrows():
        nf = numero_nf(row.get("DocumentReferenceID_2"))
        chave = (str(row["CompanyCode"]), nf)
        if chave not in notas:
            notas[chave] = (re.sub(r"\D", "", str(row.get("BPTaxNumber") or "")),
                            row.get("DocumentReferenceID_2"))

    print()
    print("=" * 70)
    print(f"XML DAS NOTAS — {len(notas)} referência(s) distinta(s) para buscar no Pipefy")
    print("=" * 70)

    encontrados, faltantes = {}, []
    for (company_code, nf), (doc_cliente, referencia) in sorted(notas.items()):
        if not nf:
            faltantes.append(f"{company_code} — título sem referência de NF")
            continue
        try:
            conteudo, motivo = xml_da_nota(company_code, nf, doc_cliente, referencia)
        except Exception as ex:
            conteudo, motivo = None, f"erro na busca ({str(ex)[:120]})"
        if conteudo:
            encontrados[f"{company_code}/NF_{nf}.xml"] = conteudo
            print(f"  ✓ {company_code} NF {nf}")
        else:
            faltantes.append(f"{company_code} NF {nf} — {motivo}")
            print(f"  ✗ {company_code} NF {nf}: {motivo}")

    resumo = {
        "total": len(notas),
        "encontrados": len(encontrados),
        "faltantes": faltantes,
        "chamadas": _PIPEFY_CHAMADAS["n"],
    }
    print(f"XMLs: {len(encontrados)} de {len(notas)} | chamadas à API do Pipefy: {_PIPEFY_CHAMADAS['n']}")

    if not encontrados:
        return None, resumo

    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as z:
        for caminho, conteudo in encontrados.items():
            z.writestr(caminho, conteudo)

    nome_zip = f"XML_SOFISA_{datetime.now().strftime('%d%m%y%H%M')}.zip"
    return {"nome": nome_zip, "conteudo": buffer.getvalue(), "mime": "application/zip"}, resumo


# ============================================================
# GERAR TODOS OS CNABS
# ============================================================

arquivos_gerados = []

resumo_empresas = []

# ============================================================
# VERIFICAR SE EXISTEM TÍTULOS PARA CRIAR
# ============================================================

if resultado_final.empty:

    mensagem_slack = (
        "ℹ️ *CNAB SOFISA — SEM TÍTULOS PARA CRIAÇÃO*\n\n"
        "Não há títulos novos para gerar."
    )

    enviar_mensagem_slack(
        mensagem_slack
    )

    print()
    print("=" * 70)
    print("SEM TÍTULOS PARA CRIAÇÃO")
    print("=" * 70)

    raise SystemExit(0)


for company_code in resultado_final["CompanyCode"].unique():

    df_empresa = resultado_final[
        resultado_final["CompanyCode"] == company_code
    ].copy()


    # ========================================================
    # GERAR CNAB
    # ========================================================

    arquivo = gerar_cnab(
        company_code,
        df_empresa
    )


    # ========================================================
    # GUARDAR ARQUIVO
    # ========================================================

    arquivos_gerados.append(
        arquivo
    )


    # ========================================================
    # GUARDAR RESUMO
    # ========================================================

    resumo_empresas.append({

        "CompanyCode":
            company_code,

        "Titulos":
            len(df_empresa),

        "Arquivo":
            arquivo["nome"],
            
        "Valor":
            arquivo["valor_total"]
    })

    valor_total_geral = sum(
        item["Valor"]
        for item in resumo_empresas
    )
    
# ============================================================
# MONTAR MENSAGEM SLACK
# ============================================================

mensagem_slack = (
    "🚀 *CNAB SOFISA GERADO COM SUCESSO*\n\n"
    "Arquivos gerados:\n"
)


for item in resumo_empresas:

    valor_formatado = (
        f"{item['Valor'] / 100:,.2f}"
        .replace(",", "X")
        .replace(".", ",")
        .replace("X", ".")
    )

    mensagem_slack += (
        f"• *{item['CompanyCode']}* — "
        f"{item['Titulos']} títulos — "
        f"R$ {valor_formatado}\n"
    )

valor_total_formatado = (
    f"{valor_total_geral / 100:,.2f}"
    .replace(",", "X")
    .replace(".", ",")
    .replace("X", ".")
)

mensagem_slack += (
    f"\n*Total de arquivos:* {len(arquivos_gerados)}"
    f"\n*Total de títulos:* "
    f"{sum(item['Titulos'] for item in resumo_empresas)}"
    f"\n*Valor total:* R$ {valor_total_formatado}"
)


# ============================================================
# XML DAS NOTAS (só roda com BUSCAR_XML_PIPEFY = True)
# Qualquer falha aqui só vira aviso: o CNAB é entregue de qualquer jeito.
# ============================================================

arquivos_slack = list(arquivos_gerados)

if BUSCAR_XML_PIPEFY:
    try:
        zip_xml, resumo_xml = montar_zip_xmls(resultado_final)
        if zip_xml:
            arquivos_slack.append(zip_xml)
        mensagem_slack += (
            f"\n\n📎 *XMLs das notas:* {resumo_xml['encontrados']} de "
            f"{resumo_xml['total']} encontrados no Pipefy"
            f" ({resumo_xml['chamadas']} chamadas à API)"
        )
        if resumo_xml["faltantes"]:
            mensagem_slack += "\nNão anexados:\n" + "\n".join(
                f"• {item}" for item in resumo_xml["faltantes"][:20])
            if len(resumo_xml["faltantes"]) > 20:
                mensagem_slack += f"\n• ... e mais {len(resumo_xml['faltantes']) - 20}"
    except Exception as ex:
        print(f"[aviso] busca de XML no Pipefy falhou: {ex}")
        mensagem_slack += (
            "\n\n⚠️ *XMLs das notas:* a busca no Pipefy falhou nesta execução "
            "(o CNAB segue normal)."
        )


# ============================================================
# ENVIAR TODOS OS ARQUIVOS EM UMA ÚNICA PUBLICAÇÃO
# ============================================================

enviar_arquivos_slack(

    arquivos_slack,

    mensagem=mensagem_slack
)

# ============================================================
# REGISTRAR TÍTULOS COMO PROCESSADOS NO CSV
# ============================================================

data_processamento = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

novos_processados = [
    {
        "Chave_Titulo": montar_chave_titulo(row),
        "HouseBank": "SOFISA",
        "Data_Processamento": data_processamento
    }
    for _, row in resultado_final.iterrows()
]

titulos_processados = salvar_titulos_processados(
    titulos_processados,
    novos_processados
)


print()
print("=" * 70)
print("CONTROLE DE TÍTULOS — CSV ATUALIZADO")
print("=" * 70)

print(
    f"Novos títulos registrados: "
    f"{len(novos_processados)}"
)

print(
    f"Total de títulos no histórico: "
    f"{len(titulos_processados)}"
)