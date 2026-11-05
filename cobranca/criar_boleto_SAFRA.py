import requests
import pandas as pd
import unicodedata
import os
import json
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

# ── mesmo arquivo e colunas usados pelo criar_boleto_BB ──────
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
    Lê titulos_processados.csv e devolve:
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

    arquivo_seq = "sequencial.txt"

    sequenciais = {}

    if os.path.exists(arquivo_seq):

        with open(arquivo_seq, "r", encoding="utf-8") as f:

            for linha in f:

                linha = linha.strip()

                if not linha or "=" not in linha:
                    continue

                empresa, numero = linha.split("=", 1)

                empresa = empresa.strip()
                numero = numero.strip()

                if numero.isdigit():
                    sequenciais[empresa] = int(numero)

    numero = sequenciais.get(company_code, 1)

    sequenciais[company_code] = numero + 1

    with open(arquivo_seq, "w", encoding="utf-8") as f:

        for empresa, valor in sequenciais.items():
            f.write(f"{empresa}={valor}\n")

    return numero


# ============================================================
# DE-PARA COMPANY CODE -> DADOS BB
# ============================================================

DEPARA_COMPANY_CODE = {
    "BR01": {
        "Nome": "EMPRESA A LTDA",
        "CNPJ": "11222333000181",
        "BB": "safra",
        "ag": "00001",
        "cc": "0000000000001"
    },
    "BR02": {
        "Nome": "EMPRESA B LTDA",
        "CNPJ": "44555666000110",
        "BB": "safra",
        "ag": "00001",
        "cc": "0000000000001"
    },
    "BR04": {
        "Nome": "EMPRESA C LTDA",
        "CNPJ": "77888999000155",
        "BB": "safra",
        "ag": "00001",
        "cc": "0000000000001"
    }
}


# ============================================================
# SLACK
# ============================================================

SLACK_URL = parametros["SLACK_URL"]
SLACK_CANAL = parametros["SLACK_CANAL"]
SLACK_TOKEN = parametros["SLACK_TOKEN"]


# ============================================================
# CONFIGURAÇÕES FIXAS - SAFRA
# ============================================================

CODIGO_BANCO = "422"
TIPO_INSCRICAO = "2"   # CNPJ
CODIGO_ARQUIVO = "1"   # 1 = Remessa
LAYOUT_ARQUIVO = "103"
NOME_BANCO = "BANCO SAFRA S/A"
HORA = "000000"


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

filtro = (
    "Customer ne '' "
    "and ClearingDate eq null "
    "and AccountingDocumentType eq 'RV' "
    f"and AssignmentReference eq 'SAFRA-{data_d1_str}' "
    "and substringof('SFR', HouseBank) eq true"
)

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

params = {
    "$filter": filtro,
    "$select": select,
    "$format": "json"
}

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
    auth=(SAP_API_1_USUARIO, SAP_API_1_SENHA),
    timeout=60
)

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

dados = response.json()

if "d" in dados and "results" in dados["d"]:
    registros = dados["d"]["results"]
elif "value" in dados:
    registros = dados["value"]
else:
    raise ValueError("Formato de retorno OData não reconhecido.")

df = pd.DataFrame(registros)

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
        timestamp = int(
            str(valor)
            .replace("/Date(", "")
            .replace(")/", "")
        )

        data = pd.to_datetime(timestamp, unit="ms")

        return data.strftime("%d.%m.%Y")

    except Exception:
        return valor


df["Dt_lcto"] = df["Dt_lcto"].apply(formatar_data_sap)
df["NetDueDate"] = df["NetDueDate"].apply(formatar_data_sap)


# ============================================================
# REMOVER DUPLICIDADES DA API 1
# ============================================================

CHAVE_API_1 = [
    "CompanyCode",
    "AccountingDocument",
    "FiscalYear",
    "AccountingDocumentItem"
]

duplicados = df[
    df.duplicated(subset=CHAVE_API_1, keep=False)
].copy()

print()
print("=" * 70)
print("API 1 - VALIDAÇÃO DE DUPLICIDADES")
print("=" * 70)

print(f"Registros recebidos pela API 1: {len(df)}")
print(f"Registros envolvidos em duplicidade: {len(duplicados)}")

if not duplicados.empty:
    print()
    print("Chaves duplicadas encontradas:")
    print(duplicados[CHAVE_API_1].drop_duplicates().to_string(index=False))

df = df.drop_duplicates(subset=CHAVE_API_1, keep="first").copy()


# ============================================================
# FILTRAR TÍTULOS JÁ PROCESSADOS
# ============================================================

# ── carrega histórico CSV (idêntico ao criar_boleto_BB) ──────
historico_processados, titulos_processados_set = (
    carregar_titulos_processados()
)

print()
print("=" * 70)
print("CONTROLE DE TÍTULOS JÁ PROCESSADOS")
print("=" * 70)

print(f"Títulos encontrados no CSV: {len(titulos_processados_set)}")

if not df.empty:

    df["ChaveProcessamento"] = df.apply(montar_chave_titulo, axis=1)

    quantidade_antes = len(df)

    df = df[
        ~df["ChaveProcessamento"].isin(titulos_processados_set)
    ].copy()

    quantidade_depois = len(df)

    print(f"Títulos recebidos após deduplicação: {quantidade_antes}")
    print(f"Títulos já processados e ignorados: {quantidade_antes - quantidade_depois}")
    print(f"Títulos novos para processamento: {quantidade_depois}")

    df = df.drop(columns=["ChaveProcessamento"], errors="ignore")

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
print(f"Registros após deduplicação: {len(df)}")

if df.empty:
    print()
    print("Nenhum registro encontrado.")
else:
    print()
    print(df.to_string(index=False))

resultado_api_1 = df


# ============================================================
# ============================================================
# API 2 - DADOS CADASTRAIS DOS CLIENTES
# ============================================================
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

if not customers:

    print()
    print("Nenhum Customer encontrado na API 1.")
    print("API 2 não será consultada.")

    resultado_api_2_tax = pd.DataFrame()
    resultado_api_2_address = pd.DataFrame()

else:

    filtro_customers = " or ".join(
        f"BusinessPartner eq '{customer}'"
        for customer in customers
    )

    SAP_API_2_BASE = SAP_API_2_URL

    # ── API 2A - DOCUMENTO FISCAL ────────────────────────────

    url_tax = f"{SAP_API_2_BASE}/A_BusinessPartnerTaxNumber"

    params_tax = {
        "$filter": (
            f"({filtro_customers}) "
            "and (BPTaxType eq 'BR1' or BPTaxType eq 'BR2')"
        ),
        "$select": "BusinessPartner,BPTaxType,BPTaxNumber",
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

    response_tax = requests.get(
        url_tax,
        params=params_tax,
        headers=headers,
        auth=(SAP_API_2_USUARIO, SAP_API_2_SENHA),
        timeout=60
    )

    print()
    print(f"HTTP STATUS: {response_tax.status_code}")
    print()
    print("URL FINAL ENVIADA:")
    print(response_tax.url)

    if response_tax.status_code != 200:
        print()
        print("CORPO DA RESPOSTA SAP:")
        print(response_tax.text)

    response_tax.raise_for_status()

    dados_tax = response_tax.json()

    if "d" in dados_tax and "results" in dados_tax["d"]:
        registros_tax = dados_tax["d"]["results"]
    elif "value" in dados_tax:
        registros_tax = dados_tax["value"]
    else:
        raise ValueError("Formato de retorno TaxNumber não reconhecido.")

    resultado_api_2_tax = pd.DataFrame(registros_tax)

    print()
    print(f"Registros fiscais encontrados: {len(resultado_api_2_tax)}")

    colunas_tax = ["BusinessPartner", "BPTaxType", "BPTaxNumber"]

    for coluna in colunas_tax:
        if coluna not in resultado_api_2_tax.columns:
            resultado_api_2_tax[coluna] = None

    resultado_api_2_tax = resultado_api_2_tax[colunas_tax]

    # ── API 2C - NOME DO CLIENTE ─────────────────────────────

    url_business_partner = f"{SAP_API_2_BASE}/A_BusinessPartner"

    params_business_partner = {
        "$filter": filtro_customers,
        "$select": "BusinessPartner,BusinessPartnerFullName",
        "$format": "json"
    }

    print()
    print("=" * 70)
    print("API 2C - NOME DO CLIENTE")
    print("=" * 70)

    print("Endpoint:")
    print(url_business_partner)

    response_business_partner = requests.get(
        url_business_partner,
        params=params_business_partner,
        headers=headers,
        auth=(SAP_API_2_USUARIO, SAP_API_2_SENHA),
        timeout=60
    )

    print()
    print(f"HTTP STATUS: {response_business_partner.status_code}")
    print()
    print("URL FINAL ENVIADA:")
    print(response_business_partner.url)

    if response_business_partner.status_code != 200:
        print()
        print("CORPO DA RESPOSTA SAP:")
        print(response_business_partner.text)

    response_business_partner.raise_for_status()

    dados_business_partner = response_business_partner.json()

    if "d" in dados_business_partner and "results" in dados_business_partner["d"]:
        registros_business_partner = dados_business_partner["d"]["results"]
    elif "value" in dados_business_partner:
        registros_business_partner = dados_business_partner["value"]
    else:
        raise ValueError("Formato de retorno BusinessPartner não reconhecido.")

    resultado_api_2_business_partner = pd.DataFrame(registros_business_partner)

    print()
    print(f"Clientes encontrados: {len(resultado_api_2_business_partner)}")

    colunas_business_partner = ["BusinessPartner", "BusinessPartnerFullName"]

    for coluna in colunas_business_partner:
        if coluna not in resultado_api_2_business_partner.columns:
            resultado_api_2_business_partner[coluna] = None

    resultado_api_2_business_partner = resultado_api_2_business_partner[colunas_business_partner]

    # ── API 2B - ENDEREÇO + E-MAIL ───────────────────────────

    url_address = f"{SAP_API_2_BASE}/A_BusinessPartnerAddress"

    params_address = {
        "$filter": filtro_customers,
        "$select": (
            "BusinessPartner,AddressID,CityName,District,"
            "HouseNumber,PostalCode,Region,StreetName,to_EmailAddress"
        ),
        "$format": "json"
    }

    print()
    print("=" * 70)
    print("API 2B - ENDEREÇO + E-MAIL")
    print("=" * 70)

    print("Endpoint:")
    print(url_address)

    response_address = requests.get(
        url_address,
        params=params_address,
        headers=headers,
        auth=(SAP_API_2_USUARIO, SAP_API_2_SENHA),
        timeout=60
    )

    print()
    print(f"HTTP STATUS: {response_address.status_code}")
    print()
    print("URL FINAL ENVIADA:")
    print(response_address.url)

    if response_address.status_code != 200:
        print()
        print("CORPO DA RESPOSTA SAP:")
        print(response_address.text)

    response_address.raise_for_status()

    dados_address = response_address.json()

    if "d" in dados_address and "results" in dados_address["d"]:
        registros_address = dados_address["d"]["results"]
    elif "value" in dados_address:
        registros_address = dados_address["value"]
    else:
        raise ValueError("Formato de retorno Address não reconhecido.")

    resultado_api_2_address = pd.DataFrame(registros_address)

    print()
    print(f"Endereços encontrados: {len(resultado_api_2_address)}")

    def extrair_email(valor):
        if not isinstance(valor, list) or len(valor) == 0:
            return None
        primeiro = valor[0]
        if isinstance(primeiro, dict):
            return primeiro.get("EmailAddress")
        return None

    if "to_EmailAddress" in resultado_api_2_address.columns:
        resultado_api_2_address["EmailAddress"] = (
            resultado_api_2_address["to_EmailAddress"].apply(extrair_email)
        )
    else:
        resultado_api_2_address["EmailAddress"] = None

    colunas_address = [
        "BusinessPartner", "AddressID", "CityName", "District",
        "HouseNumber", "PostalCode", "Region", "StreetName", "EmailAddress"
    ]

    for coluna in colunas_address:
        if coluna not in resultado_api_2_address.columns:
            resultado_api_2_address[coluna] = None

    resultado_api_2_address = resultado_api_2_address[colunas_address]


# ============================================================
# MERGE API 1 + API 2A + API 2B + API 2C
# ============================================================

if not resultado_api_1.empty:

    df_final = resultado_api_1.merge(
        resultado_api_2_tax[["BusinessPartner", "BPTaxType", "BPTaxNumber"]],
        left_on="Customer",
        right_on="BusinessPartner",
        how="left"
    ).drop(columns=["BusinessPartner"], errors="ignore")

    df_final = df_final.merge(
        resultado_api_2_address[[
            "BusinessPartner", "AddressID", "CityName", "District",
            "HouseNumber", "PostalCode", "Region", "StreetName", "EmailAddress"
        ]],
        left_on="Customer",
        right_on="BusinessPartner",
        how="left"
    ).drop(columns=["BusinessPartner"], errors="ignore")

    df_final = df_final.merge(
        resultado_api_2_business_partner[["BusinessPartner", "BusinessPartnerFullName"]],
        left_on="Customer",
        right_on="BusinessPartner",
        how="left"
    ).drop(columns=["BusinessPartner"], errors="ignore")

else:
    df_final = resultado_api_1.copy()


print()
print("=" * 70)
print("RESULTADO FINAL - API 1 + API 2A + API 2B + API 2C")
print("=" * 70)

print(f"Registros finais: {len(df_final)}")

print()

if df_final.empty:
    print("Nenhum registro para processamento.")
else:
    print(df_final.to_string(index=False))

resultado_final = df_final


# ============================================================
# FUNÇÕES AUXILIARES
# ============================================================

def limpar_numeros(valor):
    return "".join(c for c in str(valor) if c.isdigit())


def formatar_data_cnab(valor):

    if pd.isna(valor):
        return "00000000"

    valor = str(valor).strip()

    for formato in ["%d.%m.%Y", "%d/%m/%Y", "%Y-%m-%d", "%d%m%Y"]:
        try:
            return pd.to_datetime(valor, format=formato).strftime("%d%m%Y")
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


def montar_nosso_numero(row):

    base = str(row["DocumentReferenceID_2"]).split("-")[0]
    base = limpar_numeros(base)
    item = str(row["AccountingDocumentItem"]).zfill(3)
    nosso_numero = (base + item)[-9:]

    return nosso_numero.zfill(9)


def montar_seu_numero(row):

    valor = str(row["DocumentReferenceID_2"]).split("-")[0]
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

    texto = unicodedata.normalize("NFKD", str(valor))

    return "".join(
        c for c in texto if not unicodedata.combining(c)
    ).upper()


def enviar_arquivos_slack(arquivos, mensagem=None):

    headers_slack = {"Authorization": f"Bearer {SLACK_TOKEN}"}

    if not arquivos:
        raise ValueError("Nenhum arquivo foi informado para envio ao Slack.")

    arquivos_upload = []

    for arquivo in arquivos:

        nome_arquivo = arquivo["nome"]
        conteudo_bytes = arquivo["conteudo"]
        tamanho_arquivo = len(conteudo_bytes)

        print()
        print("=" * 70)
        print("SLACK - PREPARANDO ARQUIVO")
        print("=" * 70)

        print(f"Arquivo: {nome_arquivo}")
        print(f"Tamanho: {tamanho_arquivo} bytes")

        url_get_upload = "https://slack.com/api/files.getUploadURLExternal"

        params_upload = {"filename": nome_arquivo, "length": tamanho_arquivo}

        response = requests.get(
            url_get_upload,
            headers=headers_slack,
            params=params_upload,
            timeout=60
        )

        print()
        print(f"GET UPLOAD URL — HTTP STATUS: {response.status_code}")

        response.raise_for_status()

        resultado = response.json()

        if not resultado.get("ok"):
            raise RuntimeError(
                f"Erro ao obter URL de upload do Slack: {resultado}"
            )

        upload_url = resultado["upload_url"]
        file_id = resultado["file_id"]

        print(f"File ID: {file_id}")

        arquivo_memoria = BytesIO(conteudo_bytes)

        response_upload = requests.post(
            upload_url,
            files={"file": (nome_arquivo, arquivo_memoria, "text/plain")},
            timeout=60
        )

        print()
        print(f"UPLOAD STATUS: {response_upload.status_code}")

        response_upload.raise_for_status()

        arquivos_upload.append({"id": file_id, "title": nome_arquivo})

    print()
    print("=" * 70)
    print("SLACK - PUBLICANDO ARQUIVOS")
    print("=" * 70)

    url_complete = "https://slack.com/api/files.completeUploadExternal"

    data_complete = {
        "files": json.dumps(arquivos_upload),
        "channel_id": SLACK_CANAL
    }

    if mensagem:
        data_complete["initial_comment"] = mensagem

    response_complete = requests.post(
        url_complete,
        headers=headers_slack,
        data=data_complete,
        timeout=60
    )

    print()
    print(f"COMPLETE STATUS: {response_complete.status_code}")
    print(f"RESPOSTA: {response_complete.text}")

    response_complete.raise_for_status()

    resultado_final_slack = response_complete.json()

    if not resultado_final_slack.get("ok"):
        raise RuntimeError(
            f"Erro ao publicar arquivos no Slack: {resultado_final_slack}"
        )

    print()
    print("=" * 70)
    print("ARQUIVOS ENVIADOS PARA O SLACK")
    print("=" * 70)

    for arquivo in arquivos:
        print(f"✓ {arquivo['nome']}")

    return resultado_final_slack


def enviar_mensagem_slack(mensagem):

    headers_slack = {
        "Authorization": f"Bearer {SLACK_TOKEN}",
        "Content-Type": "application/json"
    }

    payload = {"channel": SLACK_CANAL, "text": mensagem}

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

    print(f"HTTP STATUS: {response.status_code}")
    print(f"RESPOSTA: {response.text}")

    response.raise_for_status()

    resultado = response.json()

    if not resultado.get("ok"):
        raise RuntimeError(
            f"Erro ao enviar mensagem para o Slack: {resultado}"
        )

    print("✓ Mensagem enviada para o Slack.")

    return resultado


# ============================================================
# GERAÇÃO DO CNAB
# ============================================================

def gerar_header(company_code, sequencia_arquivo=1):

    empresa = DEPARA_COMPANY_CODE.get(company_code)

    if empresa is None:
        raise ValueError(f"CompanyCode não parametrizado: {company_code}")

    cnpj = limpar_numeros(empresa["CNPJ"])
    agencia = str(empresa["ag"]).strip()
    conta = str(empresa["cc"]).strip()
    nome_empresa = str(empresa["Nome"]).strip()
    nsa = str(obter_proximo_nsa(company_code)).zfill(6)
    data_geracao = date.today().strftime("%d%m%Y")

    header = (
        CODIGO_BANCO
        + "00000" + " " * 9
        + TIPO_INSCRICAO
        + cnpj
        + " " * 20
        + agencia + " "
        + conta + " "
        + nome_empresa[:30].rjust(30)
        + NOME_BANCO[:30].rjust(30)
        + " " * 10
        + CODIGO_ARQUIVO
        + data_geracao
        + HORA
        + nsa
        + LAYOUT_ARQUIVO
        + " " * 5
        + " " * 20
        + " " * 20
        + " " * 29
    )

    return header


def gerar_header_lote(company_code, sequencia_arquivo=1):

    empresa = DEPARA_COMPANY_CODE.get(company_code)

    if empresa is None:
        raise ValueError(f"CompanyCode não parametrizado: {company_code}")

    cnpj = limpar_numeros(empresa["CNPJ"])
    agencia = str(empresa["ag"]).strip()
    conta = str(empresa["cc"]).strip()
    nome_empresa = str(empresa["Nome"]).strip()
    data_geracao = date.today().strftime("%d%m%Y")

    headerlote = (
        CODIGO_BANCO + "000"
        + "11R01  060 "
        + TIPO_INSCRICAO + "0"
        + cnpj
        + " " * 20
        + agencia + " "
        + conta + " "
        + nome_empresa[:30].rjust(30)
        + " " * 80
        + "00000001"
        + data_geracao
        + "00000000"
        + " " * 33
    )

    return headerlote


def gerar_segmentoP(company_code, row, contador):

    empresa = DEPARA_COMPANY_CODE[company_code]
    agencia = str(empresa["ag"]).strip()
    conta = str(empresa["cc"]).strip()

    nosso_numero = montar_nosso_numero(row)
    seu_numero = montar_seu_numero(row)
    vencimento = formatar_data_cnab(row["NetDueDate"])
    data_emissao = formatar_data_cnab(row["Dt_lcto"])
    valor = formatar_valor_cnab(row["AmountInCompanyCodeCurrency"])
    juros_diarios = calcular_juros_diarios(row["AmountInCompanyCodeCurrency"])
    referencia_empresa = montar_referencia_empresa(row)

    segmentoP = (
        "422" + "0001" + "3" + str(contador).zfill(5) + "P" + " "
        + "01" + agencia + " " + conta + " "
        + nosso_numero + " " * 11
        + "1" + "1" + "2" + "1" + "2"
        + seu_numero + " " * 5
        + vencimento + valor
        + agencia + " " + "02" + "N"
        + data_emissao + "1"
        + vencimento + juros_diarios + "0"
        + "00000000" + "0" * 15 + "0" * 15 + "0" * 15
        + referencia_empresa
        + "2" + "09" + "2" + "000" + "09" + "0000000000" + "1"
    )

    if len(segmentoP) != 240:
        raise ValueError(f"Segmento P possui {len(segmentoP)} caracteres")

    return segmentoP


def gerar_segmentoQ(company_code, row, contador):

    tpdoc = "2" if str(row["BPTaxType"]).strip().upper() == "BR1" else "1"
    documento = limpar_numeros(row["BPTaxNumber"]).zfill(15)
    nome = str(row["BusinessPartnerFullName"]).strip()[:40].ljust(40)
    endereco = (str(row["StreetName"]).strip() + " " + str(row["HouseNumber"]).strip())
    endereco = endereco[:40].ljust(40)
    bairro = str(row["District"]).strip()[:15].ljust(15)
    cep = limpar_numeros(row["PostalCode"]).zfill(8)
    cidade = str(row["CityName"]).strip()[:15].ljust(15)
    uf = str(row["Region"]).strip().upper()[:2].ljust(2)

    segmentoQ = (
        "422" + "0001" + "3" + str(contador).zfill(5) + "Q" + " "
        + "01" + tpdoc + documento + nome + endereco + bairro
        + cep[:5] + cep[5:] + cidade + uf
        + "0" + "0" * 15 + " " * 40 + "000" + " " * 20 + " " * 8
    )

    if len(segmentoQ) != 240:
        raise ValueError(f"Segmento Q possui {len(segmentoQ)} caracteres")

    return segmentoQ


def gerar_trailer_lote(quantidade_linhas, quantidade_titulos, valor_total):

    trailerlote = (
        "422" + "0001" + "5" + " " * 9
        + str(quantidade_linhas).zfill(6)
        + str(quantidade_titulos).zfill(6)
        + str(valor_total).zfill(17)
        + "0" * 6 + "0" * 17
        + "0" * 6 + "0" * 17
        + "0" * 6 + "0" * 17
        + " " * 125
    )

    if len(trailerlote) != 240:
        raise ValueError(f"Trailer lote possui {len(trailerlote)} caracteres")

    return trailerlote


def gerar_trailer(quantidade_lotes, quantidade_linhas):

    trailer = (
        "422" + "9999" + "9" + " " * 9
        + str(quantidade_lotes).zfill(6)
        + str(quantidade_linhas).zfill(6)
        + "000000" + " " * 205
    )

    if len(trailer) != 240:
        raise ValueError(f"Trailer arquivo possui {len(trailer)} caracteres")

    return trailer


def gerar_cnab(company_code, df_empresa):

    linhas = []

    linhas.append(gerar_header(company_code))
    linhas.append(gerar_header_lote(company_code))

    contador = 1
    quantidade_titulos = 0
    valor_total = 0

    for _, row in df_empresa.iterrows():

        linhas.append(gerar_segmentoP(company_code, row, contador))
        contador += 1

        linhas.append(gerar_segmentoQ(company_code, row, contador))
        contador += 1

        quantidade_titulos += 1

        valor_total += round(
            float(str(row["AmountInCompanyCodeCurrency"]).replace(",", ".")) * 100
        )

    quantidade_linhas_lote = 1 + (quantidade_titulos * 2) + 1

    linhas.append(gerar_trailer_lote(quantidade_linhas_lote, quantidade_titulos, valor_total))

    quantidade_linhas_total = len(linhas) + 1

    linhas.append(gerar_trailer(1, quantidade_linhas_total))

    hoje = datetime.now().strftime("%d%m%y%H%M")
    nome_arquivo = f"{company_code}_{hoje}.txt"

    conteudo_arquivo = ""

    for linha in linhas:
        conteudo_arquivo += remover_acentos(linha) + "\n"

    conteudo_bytes = conteudo_arquivo.encode("ascii")

    print()
    print("=" * 70)
    print("CNAB GERADO")
    print("=" * 70)
    print(f"Empresa: {company_code}")
    print(f"Arquivo: {nome_arquivo}")
    print(f"Títulos: {quantidade_titulos}")
    print(f"Linhas: {len(linhas)}")
    print(f"Valor: R$ {valor_total / 100:,.2f}")

    return {
        "nome": nome_arquivo,
        "conteudo": conteudo_bytes,
        "valor_total": valor_total
    }


# ============================================================
# GERAR TODOS OS CNABS
# ============================================================

arquivos_gerados = []
resumo_empresas = []

if resultado_final.empty:

    mensagem_slack = (
        "ℹ️ *CNAB SAFRA — SEM TÍTULOS PARA CRIAÇÃO*\n\n"
        "Não há títulos novos para gerar."
    )

    enviar_mensagem_slack(mensagem_slack)

    print()
    print("=" * 70)
    print("SEM TÍTULOS PARA CRIAÇÃO")
    print("=" * 70)

    raise SystemExit(0)


# ── novos registros para gravar no CSV ao final ──────────────
novos_processados = []

for company_code in resultado_final["CompanyCode"].unique():

    df_empresa = resultado_final[
        resultado_final["CompanyCode"] == company_code
    ].copy()

    arquivo = gerar_cnab(company_code, df_empresa)

    arquivos_gerados.append(arquivo)

    resumo_empresas.append({
        "CompanyCode": company_code,
        "Titulos": len(df_empresa),
        "Arquivo": arquivo["nome"],
        "Valor": arquivo["valor_total"]
    })

    # ── montar registros para o CSV ──────────────────────────
    data_processamento = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    for _, row in df_empresa.iterrows():
        novos_processados.append({
            "Chave_Titulo": montar_chave_titulo(row),
            "HouseBank": str(row.get("HouseBank", "SAFRA")).strip(),
            "Data_Processamento": data_processamento
        })

valor_total_geral = sum(item["Valor"] for item in resumo_empresas)


# ============================================================
# MONTAR MENSAGEM SLACK
# ============================================================

mensagem_slack = (
    "🚀 *CNAB SAFRA GERADO COM SUCESSO*\n\n"
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
    f"\n*Total de títulos:* {sum(item['Titulos'] for item in resumo_empresas)}"
    f"\n*Valor total:* R$ {valor_total_formatado}"
)


# ============================================================
# ENVIAR TODOS OS ARQUIVOS EM UMA ÚNICA PUBLICAÇÃO
# ============================================================

enviar_arquivos_slack(arquivos_gerados, mensagem=mensagem_slack)


# ============================================================
# REGISTRAR TÍTULOS COMO PROCESSADOS NO CSV
# ============================================================

historico_processados = salvar_titulos_processados(
    historico_processados,
    novos_processados
)

print()
print("=" * 70)
print("CONTROLE DE TÍTULOS — CSV ATUALIZADO")
print("=" * 70)

print(f"Novos títulos registrados: {len(novos_processados)}")
print(f"Total de títulos no histórico: {len(historico_processados)}")
