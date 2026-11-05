import requests
import pandas as pd

pd.set_option("display.max_colwidth", None)
pd.set_option("display.width", None)
import json
import os
import shutil
import uuid
from datetime import date, timedelta, datetime, timezone
from xml.sax.saxutils import escape as xml_escape
from lxml import etree
from criar_boleto_ABC import criacaoABC
from criar_boleto_BB import criacaoBB
from criar_boleto_SANTANDER import criacaoSANTANDER

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

# Serviço SOAP JournalEntryBulkChangeRequest_In - usado para gravar de volta
# no lançamento contábil o status final do processamento do boleto.
SAP_SOAP_URL = parametros["SAP_SOAP_URL"]
SAP_SOAP_USUARIO = parametros["SAP_SOAP_USUARIO"]
SAP_SOAP_SENHA = parametros["SAP_SOAP_SENHA"]


# ============================================================
# DE-PARA COMPANY CODE -> DADOS BB
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
# ============================================================
# ATUALIZAÇÃO DE STATUS NO SAP (WS-RELIABLEMESSAGING)
# ============================================================
# ============================================================
#
# O serviço JournalEntryBulkChangeRequest_In exige WS-ReliableMessaging
# com sequência EXPLÍCITA (o SAP rejeita sequência implícita com erro
# "UnknownSequence"). Por isso, para cada título é feito o handshake
# completo: CreateSequence -> mensagem de negócio -> TerminateSequence.
#
# Esta lógica foi validada em test_soap.py e é reaproveitada aqui para
# gravar, no campo customizado YY1_Status_Faturamento do item contábil
# de origem, o resultado do processamento do boleto.

WSA_NS = "http://www.w3.org/2005/08/addressing"
WSRM_NS = "http://docs.oasis-open.org/ws-rx/wsrm/200702"
SOAP_NS = "http://schemas.xmlsoap.org/soap/envelope/"

NSMAP_PARSE = {"wsrm": WSRM_NS, "soap-env": SOAP_NS}

SOAP_ACTION_STATUS_FATURAMENTO = (
    "http://sap.com/xi/SAPSCORE/SFIN/JournalEntryBulkChangeRequest_In/"
    "JournalEntryBulkChangeRequest_InRequest"
)

session_soap = requests.Session()
session_soap.auth = (SAP_SOAP_USUARIO, SAP_SOAP_SENHA)


def _abrir_sequencia_wsrm(session, endpoint_url):
    """
    Executa o CreateSequence exigido pelo SAP e retorna o Identifier
    real da sequência devolvido na resposta.
    """

    create_seq_action = f"{WSRM_NS}/CreateSequence"
    create_seq_msg_id = f"urn:uuid:{uuid.uuid4()}"

    envelope = f"""<?xml version="1.0" encoding="UTF-8"?>
<soap-env:Envelope xmlns:soap-env="{SOAP_NS}"
                    xmlns:wsa="{WSA_NS}"
                    xmlns:wsrm="{WSRM_NS}">
  <soap-env:Header>
    <wsa:To>{endpoint_url}</wsa:To>
    <wsa:Action>{create_seq_action}</wsa:Action>
    <wsa:MessageID>{create_seq_msg_id}</wsa:MessageID>
    <wsa:ReplyTo>
      <wsa:Address>{WSA_NS}/anonymous</wsa:Address>
    </wsa:ReplyTo>
  </soap-env:Header>
  <soap-env:Body>
    <wsrm:CreateSequence>
      <wsrm:AcksTo>
        <wsa:Address>{WSA_NS}/anonymous</wsa:Address>
      </wsrm:AcksTo>
    </wsrm:CreateSequence>
  </soap-env:Body>
</soap-env:Envelope>"""

    resp = session.post(
        endpoint_url,
        data=envelope.encode("utf-8"),
        headers={
            "Content-Type": "text/xml; charset=utf-8",
            "SOAPAction": create_seq_action
        },
        timeout=30
    )

    if resp.status_code != 200:
        raise RuntimeError(
            f"CreateSequence falhou (HTTP {resp.status_code}): {resp.text[:500]}"
        )

    root = etree.fromstring(resp.content)

    identifier_el = root.find(
        ".//wsrm:CreateSequenceResponse/wsrm:Identifier",
        namespaces=NSMAP_PARSE
    )

    if identifier_el is None:
        raise RuntimeError(
            f"Não foi possível extrair wsrm:Identifier da resposta: {resp.text[:500]}"
        )

    return identifier_el.text


def _enviar_mensagem_status_faturamento(session, endpoint_url, sequence_id,
                                         company_code, accounting_document,
                                         fiscal_year, itens):
    """
    Envia UMA mensagem JournalEntryBulkChangeRequestMessage atualizando o
    YY1_Status_Faturamento de TODOS os itens informados do mesmo documento.

    itens: lista de (accounting_document_item_id, valor_status)

    Tudo numa mensagem só porque o SAP bloqueia o documento inteiro ao
    alterar um item: mensagens separadas para itens do mesmo documento
    chegam em paralelo e só a primeira é aplicada.
    """

    wsa_message_id = f"urn:uuid:{uuid.uuid4()}"
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    message_id = f"BOLETO_{uuid.uuid4().hex[:8].upper()}"

    # Escapa tudo que pode conter caracteres especiais de XML (ex.: & < >
    # em mensagens de erro do SAP/BB/ABC que acabam indo para o valor).
    company_code_xml = xml_escape(str(company_code))
    accounting_document_xml = xml_escape(str(accounting_document))
    fiscal_year_xml = xml_escape(str(fiscal_year))

    blocos_itens = []

    for numero, (item_id, valor_status) in enumerate(itens, 1):
        blocos_itens.append(f"""      <JournalEntryDebtorCreditorItem>
        <MessageHeader>
          <ID>{message_id}_ITEM{numero}</ID>
          <CreationDateTime>{now}</CreationDateTime>
        </MessageHeader>
        <ItemKey>
          <AccountingDocument>{accounting_document_xml}</AccountingDocument>
          <CompanyCode>{company_code_xml}</CompanyCode>
          <FiscalYear>{fiscal_year_xml}</FiscalYear>
          <AccountingDocumentItemID>{xml_escape(str(item_id))}</AccountingDocumentItemID>
        </ItemKey>
        <yy1:YY1_Status_Faturamento>{xml_escape(str(valor_status))}</yy1:YY1_Status_Faturamento>
      </JournalEntryDebtorCreditorItem>""")

    itens_xml = "\n".join(blocos_itens)

    envelope = f"""<?xml version="1.0" encoding="UTF-8"?>
<soap-env:Envelope xmlns:soap-env="{SOAP_NS}"
                    xmlns:sfin="http://sap.com/xi/SAPSCORE/SFIN"
                    xmlns:yy1="http://SAPCustomFields.com/YY1_"
                    xmlns:wsa="{WSA_NS}"
                    xmlns:wsrm="{WSRM_NS}">
  <soap-env:Header>
    <wsa:To>{endpoint_url}</wsa:To>
    <wsa:Action>{SOAP_ACTION_STATUS_FATURAMENTO}</wsa:Action>
    <wsa:MessageID>{wsa_message_id}</wsa:MessageID>
    <wsa:ReplyTo>
      <wsa:Address>{WSA_NS}/anonymous</wsa:Address>
    </wsa:ReplyTo>
    <wsrm:Sequence>
      <wsrm:Identifier>{sequence_id}</wsrm:Identifier>
      <wsrm:MessageNumber>1</wsrm:MessageNumber>
    </wsrm:Sequence>
  </soap-env:Header>
  <soap-env:Body>
    <sfin:JournalEntryBulkChangeRequestMessage>
      <MessageHeader>
        <ID>{message_id}</ID>
        <CreationDateTime>{now}</CreationDateTime>
      </MessageHeader>
{itens_xml}
    </sfin:JournalEntryBulkChangeRequestMessage>
  </soap-env:Body>
</soap-env:Envelope>"""

    return session.post(
        endpoint_url,
        data=envelope.encode("utf-8"),
        headers={
            "Content-Type": "text/xml; charset=utf-8",
            "SOAPAction": SOAP_ACTION_STATUS_FATURAMENTO
        },
        timeout=30
    )


def _fechar_sequencia_wsrm(session, endpoint_url, sequence_id):
    """
    Encerra a sequência WS-RM (boa prática). Uma falha aqui não deve
    interromper o processamento - a mensagem de negócio já foi enviada.
    """

    terminate_action = f"{WSRM_NS}/TerminateSequence"
    terminate_msg_id = f"urn:uuid:{uuid.uuid4()}"

    envelope = f"""<?xml version="1.0" encoding="UTF-8"?>
<soap-env:Envelope xmlns:soap-env="{SOAP_NS}"
                    xmlns:wsa="{WSA_NS}"
                    xmlns:wsrm="{WSRM_NS}">
  <soap-env:Header>
    <wsa:To>{endpoint_url}</wsa:To>
    <wsa:Action>{terminate_action}</wsa:Action>
    <wsa:MessageID>{terminate_msg_id}</wsa:MessageID>
    <wsa:ReplyTo>
      <wsa:Address>{WSA_NS}/anonymous</wsa:Address>
    </wsa:ReplyTo>
  </soap-env:Header>
  <soap-env:Body>
    <wsrm:TerminateSequence>
      <wsrm:Identifier>{sequence_id}</wsrm:Identifier>
    </wsrm:TerminateSequence>
  </soap-env:Body>
</soap-env:Envelope>"""

    try:
        session.post(
            endpoint_url,
            data=envelope.encode("utf-8"),
            headers={
                "Content-Type": "text/xml; charset=utf-8",
                "SOAPAction": terminate_action
            },
            timeout=30
        )
    except Exception:
        # Não faz sentido derrubar o processamento por causa do
        # encerramento "de cortesia" da sequência.
        pass


def atualizar_status_faturamento_sap(session, endpoint_url, company_code,
                                      accounting_document, fiscal_year, itens):
    """
    Executa o ciclo completo (CreateSequence -> mensagem de negócio ->
    TerminateSequence) para gravar o status do processamento do boleto
    de volta no campo YY1_Status_Faturamento do lançamento contábil.

    Retorna sempre um dicionário, nunca lança exceção:
        {"sucesso": bool, "status_code": int|None, "mensagem": str}
    """

    try:
        sequence_id = _abrir_sequencia_wsrm(session, endpoint_url)

    except Exception as erro:
        return {
            "sucesso": False,
            "status_code": None,
            "mensagem": f"Falha no CreateSequence: {erro}"
        }

    try:
        resp = _enviar_mensagem_status_faturamento(
            session,
            endpoint_url,
            sequence_id,
            company_code,
            accounting_document,
            fiscal_year,
            itens
        )

    except Exception as erro:
        _fechar_sequencia_wsrm(session, endpoint_url, sequence_id)

        return {
            "sucesso": False,
            "status_code": None,
            "mensagem": f"Falha ao enviar mensagem SOAP: {erro}"
        }

    _fechar_sequencia_wsrm(session, endpoint_url, sequence_id)

    corpo_resposta = resp.text[:1000] if resp.text else ""

    # ========================================================
    # PROCURAR SOAP FAULT DENTRO DO CORPO
    # ========================================================
    # Esse serviço processa a mensagem de forma assíncrona: o SAP pode
    # responder HTTP 200 apenas confirmando o RECEBIMENTO da mensagem,
    # e um erro de negócio (ex.: ItemKey não encontrado, autorização)
    # aparece dentro de um <soap:Fault> no corpo, ou nem aparece aqui -
    # só no Web Service Error Log (transação SRT_MONI) do SAP.

    fault_encontrado = None

    try:
        root_resp = etree.fromstring(resp.content)

        fault_el = root_resp.find(
            ".//soap-env:Fault",
            namespaces=NSMAP_PARSE
        )

        if fault_el is not None:
            faultstring_el = fault_el.find(".//faultstring")

            if faultstring_el is None:
                # SOAP 1.1 usa <faultstring> sem namespace
                for filho in fault_el.iter():
                    if filho.tag.endswith("faultstring"):
                        faultstring_el = filho
                        break

            fault_encontrado = (
                faultstring_el.text
                if faultstring_el is not None
                else "SOAP Fault recebido, sem faultstring detalhado."
            )

    except Exception:
        # Corpo não é XML válido ou não pôde ser parseado -
        # não impede o fluxo, só não conseguimos inspecionar o fault.
        pass

    if resp.status_code != 200:
        return {
            "sucesso": False,
            "status_code": resp.status_code,
            "mensagem": fault_encontrado or corpo_resposta or "Falha sem corpo de resposta."
        }

    if fault_encontrado:
        return {
            "sucesso": False,
            "status_code": resp.status_code,
            "mensagem": f"SOAP Fault: {fault_encontrado}"
        }

    return {
        "sucesso": True,
        "status_code": resp.status_code,
        "mensagem": (
            "Mensagem aceita pelo SAP (HTTP 200, sem Fault na resposta). "
            "Serviço é assíncrono - confirme a aplicação real no "
            "Web Service Error Log (SRT_MONI) do SAP. "
            f"Corpo: {corpo_resposta}"
        )
    }


# ============================================================
# DATA D
# ============================================================

data_d1 = date.today() - timedelta(days=0)

# AssignmentReference está no formato DD/MM/AAAA
data_d1_str = data_d1.strftime("%d/%m/%Y")
# AssignmentReference está no formato DD/MM/AA
data_d2_str = data_d1.strftime("%d/%m/%y")

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
    "and (AccountingDocumentType eq 'RV' or AccountingDocumentType eq 'DR') "
    f"and (AssignmentReference eq '{data_d1_str}' "
    f"or AssignmentReference eq 'ABC-{data_d1_str}' "
    f"or AssignmentReference eq 'SANTANDER-{data_d2_str}')"
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
# DISTRIBUIÇÃO DOS PDFs DO ABC
#
# O ABC devolve o boleto como PDF (não há URL pública como no BB).
# O criar_boleto_ABC.py salva o PDF na pasta do código, que os
# usuários não acessam. Aqui o PDF é copiado para uma pasta de rede
# configurada no parametros_prod.txt:
#
#   PASTA_PDF_COMPARTILHADA=\\servidor\Financeiro\Boletos ABC
#
# Os PDFs vão para uma subpasta por data (aaaa-mm-dd).
# Vazio/ausente = não copia (aviso no log).
#
# Falhas aqui NUNCA interrompem o processamento: o boleto já foi
# registrado no banco; só a cópia do arquivo falhou (fica no log).
# ============================================================

PASTA_PDF_COMPARTILHADA = parametros.get(
    "PASTA_PDF_COMPARTILHADA", ""
).strip()

def copiar_pdf_compartilhado(pdf_origem, nome_destino):
    """Copia o PDF para PASTA_PDF_COMPARTILHADA/aaaa-mm-dd/.
    Devolve o caminho copiado, ou None se não configurado/falhou."""

    if not pdf_origem:
        return None

    if not PASTA_PDF_COMPARTILHADA:
        print(
            "AVISO: PASTA_PDF_COMPARTILHADA não configurada no "
            "parametros_prod.txt - PDF ficou só na pasta do código."
        )
        return None

    try:

        pasta_dia = os.path.join(
            PASTA_PDF_COMPARTILHADA,
            date.today().strftime("%Y-%m-%d")
        )

        os.makedirs(pasta_dia, exist_ok=True)

        destino = os.path.join(pasta_dia, nome_destino)

        shutil.copy2(pdf_origem, destino)

        print(f"PDF disponibilizado em: {destino}")

        return destino

    except Exception as erro:

        print(f"AVISO: não foi possível copiar o PDF para a pasta compartilhada: {erro}")

        return None


def nome_amigavel_pdf(nome_cliente, referencia, item):
    """Ex.: 'CLIENTE EXEMPLO LTDA - 001234567-8 - 002.pdf'
    (sem caracteres inválidos para Windows/SharePoint)."""

    texto = f"{nome_cliente or 'CLIENTE'} - {referencia} - {item}"

    texto = "".join(
        "_" if c in '<>:"/\\|?*#%' else c
        for c in str(texto)
    ).strip()

    return f"{texto[:150]}.pdf"


# ============================================================
# AVALIAÇÃO DA RESPOSTA DE CRIAÇÃO DO BOLETO
#
# Cada banco sinaliza sucesso de um jeito:
#
#   BB  -> HTTP 201, campos "numero", "observacao",
#          "urlImagemBoleto"; erros em "erros": [...]
#
#   ABC -> HTTP 200 com "sucesso": true e
#          desenvolvedor.codigoRetorno == "0" (TEXTO).
#          Não devolve URL; identificamos o boleto pela
#          linha digitável. Erro vem em "desenvolvedor".
#
# Por isso a decisão de sucesso/erro passa por aqui, e não
# por `status_code == 201` (que só vale para o BB).
# ============================================================

def avaliar_resposta(house_bank, resposta):

    status_code = resposta.get("status_code")
    resposta_json = resposta.get("resposta")
    payload = resposta.get("payload") or {}

    if not isinstance(resposta_json, dict):
        resposta_json = {}

    resultado = {
        "sucesso": False,
        "numero_boleto": None,
        "observacao": None,
        "url": None,
        "nome_pagador": None,
        "cnpj_pagador": None,
        "codigo_erro": None,
        "mensagem_erro": None,
        "ocorrencia": None,
        "pix": None,
        "pdf_arquivo": None,
        "nosso_numero": None,
        "linha_digitavel": None,
        "erro_referencia": False,
    }

    # --------------------------------------------------------
    # SANTANDER
    #
    # HTTP 201 com digitableLine/barcode. Não devolve URL nem
    # PDF; o boleto é identificado pela linha digitável.
    # Erros vêm normalizados em "erros" pelo criar_boleto_SANTANDER.
    # --------------------------------------------------------

    if house_bank.startswith("SAN"):

        if status_code == 201 and resposta.get("linha_digitavel"):

            resultado.update({
                "sucesso": True,
                "numero_boleto": resposta.get("nosso_numero"),
                "nosso_numero": resposta.get("nosso_numero"),
                "linha_digitavel": resposta.get("linha_digitavel"),
                "nome_pagador": resposta.get("nome_pagador"),
                "cnpj_pagador": resposta.get("cnpj_pagador"),
                "pix": resposta.get("pix"),
                "pdf_arquivo": resposta.get("pdf_arquivo"),
            })

        else:

            erros = resposta_json.get("erros") or []
            primeiro = erros[0] if erros and isinstance(erros[0], dict) else {}

            resultado.update({
                "codigo_erro": primeiro.get("codigo") or status_code,
                "mensagem_erro": (
                    primeiro.get("mensagem")
                    or resposta_json.get("_message")
                    or f"HTTP {status_code}"
                ),
                "ocorrencia": primeiro.get("ocorrencia"),
                "erro_referencia": bool(resposta.get("erro_referencia")),
            })

        return resultado

    # --------------------------------------------------------
    # BANCO ABC
    # --------------------------------------------------------

    if house_bank.startswith("ABC"):

        desenvolvedor = resposta_json.get("desenvolvedor") or {}

        codigo_retorno = str(
            desenvolvedor.get("codigoRetorno", "")
        ).strip()

        sucesso = (
            status_code == 200
            and resposta_json.get("sucesso") is True
            and codigo_retorno == "0"
        )

        if sucesso:

            resultado.update({
                "sucesso": True,
                "numero_boleto": resposta_json.get("linhaDigitavel"),
                "url": None,
                "pdf_arquivo": resposta.get("pdf_arquivo"),
                "linha_digitavel": resposta_json.get("linhaDigitavel"),
                "nome_pagador": payload.get("nome"),
                "cnpj_pagador": payload.get("cnpjCPF"),
                "pix": resposta.get("pix"),
            })

        else:

            resultado.update({
                "codigo_erro": codigo_retorno or status_code,
                "mensagem_erro": (
                    desenvolvedor.get("mensagem")
                    or resposta_json.get("mensagem")
                    or f"HTTP {status_code}"
                ),
            })

        return resultado

    # --------------------------------------------------------
    # BANCO DO BRASIL
    # --------------------------------------------------------

    if status_code == 201:

        pagador = payload.get("pagador", {}) or {}

        resultado.update({
            "sucesso": True,
            "numero_boleto": resposta_json.get("numero"),
            "nosso_numero": resposta_json.get("numero"),
            "linha_digitavel": resposta_json.get("linhaDigitavel"),
            "observacao": resposta_json.get("observacao"),
            "url": resposta_json.get("urlImagemBoleto"),
            "nome_pagador": pagador.get("nome"),
            "cnpj_pagador": pagador.get("numeroInscricao"),
        })

        return resultado

    erros = resposta_json.get("erros")

    if isinstance(erros, list) and erros:

        primeiro_erro = erros[0]

        resultado.update({
            "codigo_erro": primeiro_erro.get("codigo"),
            "mensagem_erro": primeiro_erro.get("mensagem"),
            "ocorrencia": primeiro_erro.get("ocorrencia"),
        })

    else:

        resultado["mensagem_erro"] = (
            resposta_json.get("mensagem")
            or f"HTTP {status_code}"
        )

    return resultado


# ============================================================
# CRIAÇÃO DOS BOLETOS + STATUS POR TÍTULO
# ============================================================

if resultado_final.empty:

    print()
    print("=" * 100)
    print("STATUS FINAL")
    print("=" * 100)
    print("SEM_PENDENCIAS")
    print("Nenhum título retornado pelo SAP.")

else:

    # ========================================================
    # COLUNAS DO RELATÓRIO
    # ========================================================

    resultado_final["Status_Processamento"] = None
    resultado_final["Numero_Boleto"] = None
    resultado_final["Observacao"] = None
    resultado_final["Codigo_Erro"] = None
    resultado_final["Mensagem_Erro"] = None
    resultado_final["Ocorrencia"] = None
    resultado_final["Pix"] = None
    resultado_final["Nosso_Numero"] = None
    resultado_final["Linha_Digitavel"] = None
    resultado_final["SAP_Atualizado"] = None
    resultado_final["SAP_Mensagem_Atualizacao"] = None
    boletos_gerados_slack = []
    titulos_processados_agora = []

    # Atualizações do YY1_Status_Faturamento acumuladas durante o loop
    # e enviadas no fim, UMA mensagem por documento contábil.
    fila_sap = []
    
    # ========================================================
    # ARQUIVO DE TÍTULOS JÁ PROCESSADOS
    # ========================================================

    ARQUIVO_TITULOS_PROCESSADOS = "titulos_processados.csv"

    COLUNAS_HISTORICO = [
        "Chave_Titulo",
        "HouseBank",
        "Data_Processamento"
    ]


    # ========================================================
    # CARREGAR HISTÓRICO DE TÍTULOS PROCESSADOS
    # ========================================================

    try:

        historico_processados = pd.read_csv(
            ARQUIVO_TITULOS_PROCESSADOS,
            dtype=str
        )

        # Garantir todas as colunas
        for coluna in COLUNAS_HISTORICO:

            if coluna not in historico_processados.columns:
                historico_processados[coluna] = ""

        historico_processados = historico_processados[
            COLUNAS_HISTORICO
        ].fillna("")


    except FileNotFoundError:

        historico_processados = pd.DataFrame(
            columns=COLUNAS_HISTORICO
        )


    # ========================================================
    # SET COM AS CHAVES DOS TÍTULOS JÁ GERADOS
    # ========================================================

    titulos_processados = set(
        historico_processados[
            "Chave_Titulo"
        ]
        .astype(str)
        .str.strip()
    )


    # ========================================================
    # LISTA PARA NOVOS REGISTROS DESTA EXECUÇÃO
    # ========================================================

    novos_processados = []

    
    # ========================================================
    # PROCESSAR CADA TÍTULO
    # ========================================================
 
    for indice, linha in resultado_final.iterrows():

        try:

            # =========================================================
            # IDENTIFICAR BANCO
            # =========================================================

            house_bank = str(
                linha["HouseBank"]
            ).strip()


            # =========================================================
            # IDENTIFICAR TÍTULO
            # =========================================================

            chave_titulo = (
                str(linha["CompanyCode"]).strip()
                + "|"
                + str(linha["AccountingDocument"]).strip()
                + "|"
                + str(linha["FiscalYear"]).strip()
                + "|"
                + str(linha["AccountingDocumentItem"]).strip()
            )


            # =========================================================
            # VERIFICAR SE JÁ FOI PROCESSADO
            # =========================================================

            if chave_titulo in titulos_processados:

                continue


            # =========================================================
            # BANCO DO BRASIL
            # =========================================================

            if house_bank in [
                "BB002",
                "BB003",
                "BB004"
            ]:

                resposta = criacaoBB(linha)


            # =========================================================
            # BANCO ABC
            # =========================================================

            elif house_bank in [
                "ABC01"
            ]:

                resposta = criacaoABC(linha)


            # =========================================================
            # SANTANDER
            # =========================================================

            elif house_bank in [
                "SAN01"
            ]:

                resposta = criacaoSANTANDER(linha)
                
                
            # =========================================================
            # HOUSE BANK NÃO PARAMETRIZADO
            # =========================================================

            else:

                continue

 
 
            # =========================================================
            # DADOS RETORNADOS PELA CRIAÇÃO
            # =========================================================

            status_code = resposta.get(
                "status_code"
            )

            resposta_json = resposta.get(
                "resposta"
            )

            payload = resposta.get(
                "payload"
            )


            avaliacao = avaliar_resposta(
                house_bank,
                resposta
            )


            # =========================================================
            # SUCESSO (BB: HTTP 201 | ABC: HTTP 200 + sucesso=true)
            # =========================================================

            if avaliacao["sucesso"]:

                numero_boleto = avaliacao["numero_boleto"]
                observacao = avaliacao["observacao"]


                # -----------------------------------------------------
                # COM OBSERVAÇÃO
                # -----------------------------------------------------

                if observacao:

                    status = (
                        "CONCLUIDA_COM_OBSERVAÇÕES"
                    )

                # -----------------------------------------------------
                # SEM OBSERVAÇÃO
                # -----------------------------------------------------

                else:

                    status = "CONCLUIDA"


                resultado_final.at[
                    indice,
                    "Status_Processamento"
                ] = status

                resultado_final.at[
                    indice,
                    "Numero_Boleto"
                ] = numero_boleto

                resultado_final.at[
                    indice,
                    "Observacao"
                ] = observacao

                resultado_final.at[
                    indice,
                    "Pix"
                ] = "SIM" if avaliacao.get("pix") else "NAO"

                resultado_final.at[indice, "Nosso_Numero"] = avaliacao.get("nosso_numero")
                resultado_final.at[indice, "Linha_Digitavel"] = avaliacao.get("linha_digitavel")

                # =====================================================
                # ATUALIZAR STATUS NO SAP (YY1_STATUS_FATURAMENTO)
                # =====================================================
                # Mensagens longas não são aplicadas pelo SAP (limite de
                # tamanho do campo customizado). Por isso o valor enviado
                # é sempre um destes três, curtos e fixos:
                # "EMITIDO", "ERRO NA REFERENCIA" ou "ERRO".

                valor_status_sap = "EMITIDO"

                # O ItemKey do serviço SOAP espera o item SEM zero à
                # esquerda (ex.: "1"), mas o OData devolve com zero à
                # esquerda (ex.: "001"). Convertendo só para a chamada
                # SOAP - a reconsulta OData continua usando o valor
                # original.
                item_id_soap = str(int(linha["AccountingDocumentItem"]))

                # Enviado ao SAP no fim, agrupado por documento
                fila_sap.append((indice, linha, item_id_soap, valor_status_sap))

                # =====================================================
                # GRAVAR TÍTULO COMO PROCESSADO
                # =====================================================
                data_processamento = pd.Timestamp.now().strftime(
                    "%Y-%m-%d %H:%M:%S"
                )

                novo_registro = {

                    "Chave_Titulo":
                        chave_titulo,

                    "HouseBank":
                        house_bank,

                    "Data_Processamento":
                        data_processamento,

                }

                novos_processados.append(
                    novo_registro
                )

                # =====================================================
                # ATUALIZAR SET EM MEMÓRIA
                # =====================================================

                titulos_processados.add(
                    chave_titulo
                )

                titulos_processados_agora.append(
                    indice
                )

                # =====================================================
                # DADOS PARA O SLACK
                #
                # BB  -> link do boleto (urlImagemBoleto)
                # ABC -> sem URL pública; vai a linha digitável
                # =====================================================

                if avaliacao["url"] or avaliacao["numero_boleto"]:

                    boletos_gerados_slack.append({

                        "nome": avaliacao["nome_pagador"],

                        "cnpj": avaliacao["cnpj_pagador"],

                        "referencia": (
                            f"{linha['DocumentReferenceID_2']} -> "
                            f"{linha['AccountingDocumentItem']}"
                        ),

                        "url": avaliacao["url"],

                        "numero": avaliacao["numero_boleto"],

                        "linha_digitavel": avaliacao.get("linha_digitavel"),

                        "pdf": avaliacao.get("pdf_arquivo"),

                        "nome_pdf": nome_amigavel_pdf(
                            avaliacao["nome_pagador"],
                            linha["DocumentReferenceID_2"],
                            linha["AccountingDocumentItem"]
                        )
                    })

                    # -------------------------------------------------
                    # PDF (ABC): disponibilizar fora da pasta do código
                    # -------------------------------------------------

                    if avaliacao.get("pdf_arquivo"):

                        boletos_gerados_slack[-1]["pdf_compartilhado"] = (
                            copiar_pdf_compartilhado(
                                avaliacao["pdf_arquivo"],
                                boletos_gerados_slack[-1]["nome_pdf"]
                            )
                        )


            # =========================================================
            # ERRO (regra de cada banco em avaliar_resposta)
            # =========================================================

            else:

                codigo_erro = avaliacao["codigo_erro"]
                mensagem_erro = avaliacao["mensagem_erro"]
                ocorrencia = avaliacao["ocorrencia"]


                # =========================================================
                # GRAVAR ERRO NO RELATÓRIO
                # =========================================================

                resultado_final.at[
                    indice,
                    "Status_Processamento"
                ] = "ERRO_EXECUCAO"

                resultado_final.at[
                    indice,
                    "Codigo_Erro"
                ] = codigo_erro

                resultado_final.at[
                    indice,
                    "Mensagem_Erro"
                ] = mensagem_erro

                resultado_final.at[
                    indice,
                    "Ocorrencia"
                ] = ocorrencia

                # =========================================================
                # ATUALIZAR STATUS NO SAP (YY1_STATUS_FATURAMENTO)
                # =========================================================
                # Mensagens longas não são aplicadas pelo SAP (limite de
                # tamanho do campo customizado). Por isso o valor enviado
                # é sempre um destes três, curtos e fixos:
                # "EMITIDO", "ERRO NA REFERENCIA" ou "ERRO".
                #
                # "ERRO NA REFERENCIA" é usado especificamente quando o BB
                # recusa por Nosso Número já incluído anteriormente
                # (código 4874915) - qualquer outro erro cai em "ERRO".

                # BB: código 4874915 | Santander: nosso número duplicado
                # após esgotar as retentativas do criar_boleto_SANTANDER
                if str(codigo_erro) == "4874915" or avaliacao.get("erro_referencia"):
                    valor_status_sap = "ERRO NA REFERENCIA"
                else:
                    valor_status_sap = "ERRO"

                item_id_soap = str(int(linha["AccountingDocumentItem"]))

                # Enviado ao SAP no fim, agrupado por documento
                fila_sap.append((indice, linha, item_id_soap, valor_status_sap))

                # =========================================================
                # MARCAR COMO PROCESSADO NESTA EXECUÇÃO (PARA O SLACK)
                # =========================================================

                titulos_processados_agora.append(
                    indice
                )

        except Exception as erro:

            resultado_final.at[
                indice,
                "Status_Processamento"
            ] = "ERRO_EXECUCAO"

            resultado_final.at[
                indice,
                "Mensagem_Erro"
            ] = str(erro)

            # =========================================================
            # ATUALIZAR STATUS NO SAP MESMO EM CASO DE EXCEÇÃO INESPERADA
            # =========================================================

            try:
                item_id_soap = str(int(linha["AccountingDocumentItem"]))
            except Exception:
                item_id_soap = linha["AccountingDocumentItem"]

            # Enviado ao SAP no fim, agrupado por documento
            fila_sap.append((indice, linha, item_id_soap, "ERRO"))

            # =========================================================
            # MARCAR COMO PROCESSADO NESTA EXECUÇÃO (PARA O SLACK)
            # =========================================================

            titulos_processados_agora.append(
                indice
            )

    # ========================================================
    # ATUALIZAR STATUS NO SAP — UMA MENSAGEM POR DOCUMENTO
    #
    # Itens do mesmo documento (ex.: parcelas 004, 005, 006 do
    # 9448924076) vão juntos. Mensagens separadas chegavam em
    # paralelo, o SAP bloqueava o documento na primeira e as
    # demais não eram aplicadas.
    # ========================================================

    grupos_sap = {}

    for indice_fila, linha_fila, item_fila, valor_fila in fila_sap:

        chave_documento = (
            str(linha_fila["CompanyCode"]).strip(),
            str(linha_fila["AccountingDocument"]).strip(),
            str(linha_fila["FiscalYear"]).strip(),
        )

        grupos_sap.setdefault(chave_documento, []).append(
            (indice_fila, item_fila, valor_fila)
        )

    if grupos_sap:

        print()
        print("=" * 120)
        print("ATUALIZAÇÃO DO STATUS NO SAP (agrupada por documento)")
        print("=" * 120)

    for (empresa, documento, exercicio), itens_grupo in grupos_sap.items():

        try:
            resultado_sap = atualizar_status_faturamento_sap(
                session_soap,
                SAP_SOAP_URL,
                empresa,
                documento,
                exercicio,
                [(item, valor) for _, item, valor in itens_grupo],
            )
        except Exception as erro_sap:
            resultado_sap = {
                "sucesso": False,
                "mensagem": f"Falha ao atualizar o SAP: {erro_sap}",
            }

        descricao_itens = ", ".join(f"{item}={valor}" for _, item, valor in itens_grupo)

        print(
            f"{empresa}/{documento}/{exercicio} -> itens {descricao_itens}: "
            f"{'OK' if resultado_sap['sucesso'] else 'FALHOU'}"
        )

        for indice_grupo, item, valor in itens_grupo:

            resultado_final.at[
                indice_grupo,
                "SAP_Atualizado"
            ] = "Sim" if resultado_sap["sucesso"] else "Não"

            resultado_final.at[
                indice_grupo,
                "SAP_Mensagem_Atualizacao"
            ] = resultado_sap["mensagem"]

        if not resultado_sap["sucesso"]:

            print(
                f"AVISO: falha ao atualizar YY1_Status_Faturamento "
                f"no SAP para {empresa}/{documento}/{exercicio}: "
                f"{resultado_sap['mensagem']}"
            )

    # ========================================================
    # SALVAR HISTÓRICO DE TÍTULOS PROCESSADOS
    # ========================================================

    if novos_processados:

        novos_df = pd.DataFrame(
            novos_processados,
            columns=COLUNAS_HISTORICO
        )


        # ====================================================
        # ADICIONAR AO HISTÓRICO EXISTENTE
        # ====================================================

        historico_processados = pd.concat(
            [
                historico_processados,
                novos_df
            ],
            ignore_index=True
        )


        # ====================================================
        # GARANTIR UMA LINHA POR TÍTULO
        # ====================================================

        historico_processados = (
            historico_processados
            .drop_duplicates(
                subset=["Chave_Titulo"],
                keep="last"
            )
        )


        # ====================================================
        # SALVAR CSV
        # ====================================================

        historico_processados.to_csv(
            ARQUIVO_TITULOS_PROCESSADOS,
            index=False,
            encoding="utf-8-sig"
        )


        print()
        print("=" * 120)
        print("HISTÓRICO DE TÍTULOS PROCESSADOS")
        print("=" * 120)

        print(
            f"Novos títulos registrados: "
            f"{len(novos_df)}"
        )

        print(
            f"Total de títulos no histórico: "
            f"{len(historico_processados)}"
        )

    else:

        print()
        print("=" * 120)
        print("HISTÓRICO DE TÍTULOS PROCESSADOS")
        print("=" * 120)

        print(
            "Nenhum novo título gerado com sucesso."
        )

    # ========================================================
    # RELATÓRIO FINAL
    # ========================================================

    print()
    print("=" * 120)
    print("RELATÓRIO FINAL - STATUS POR TÍTULO")
    print("=" * 120)

    colunas_relatorio = [
        "HouseBank",
        "CompanyCode",
        "AccountingDocument",
        "FiscalYear",
        "AccountingDocumentItem",
        "Customer",
        "AmountInCompanyCodeCurrency",
        "NetDueDate",
        "Status_Processamento",
        "Numero_Boleto",
        "Pix",
        "Nosso_Numero",
        "Linha_Digitavel",
        "Observacao",
        "Codigo_Erro",
        "Mensagem_Erro",
        "Ocorrencia",
        "SAP_Atualizado",
        "SAP_Mensagem_Atualizacao"
    ]

    print()

    print(
        resultado_final[
            colunas_relatorio
        ].to_string(index=False)
    )
    
    # ========================================================
    # RELATÓRIO SLACK
    # ========================================================

    if not titulos_processados_agora:

        print()
        print("=" * 120)
        print("SLACK - NENHUM NOVO TÍTULO PROCESSADO")
        print("Nenhuma mensagem será enviada.")
        print("=" * 120)

    else:

        # ========================================================
        # CONFIGURAÇÃO SLACK
        # ========================================================

        SLACK_URL = parametros["SLACK_URL"]
        SLACK_CANAL = parametros["SLACK_CANAL"]
        SLACK_TOKEN = parametros["SLACK_TOKEN"]


        # ========================================================
        # SOMENTE TÍTULOS DESTA EXECUÇÃO
        # ========================================================

        resultado_slack = resultado_final.loc[
            titulos_processados_agora
        ]


        total_titulos = len(
            resultado_slack
        )


        total_sucesso = (
            resultado_slack[
                "Status_Processamento"
            ].isin([
                "CONCLUIDA",
                "CONCLUIDA_COM_OBSERVAÇÕES"
            ]).sum()
        )


        total_erros = (
            resultado_slack[
                "Status_Processamento"
            ].eq("ERRO_EXECUCAO").sum()
        )


        # ========================================================
        # PERCENTUAL DE SUCESSO
        # ========================================================

        percentual_sucesso = (
            total_sucesso
            / total_titulos
            * 100
        )


        # ========================================================
        # PRINCIPAL MENSAGEM DE ERRO
        # ========================================================

        mensagens_erro = (
            resultado_slack.loc[
                resultado_slack[
                    "Status_Processamento"
                ] == "ERRO_EXECUCAO",
                "Mensagem_Erro"
            ]
            .dropna()
            .astype(str)
        )


        if not mensagens_erro.empty:

            mensagem_erro_principal = (
                mensagens_erro
                .value_counts()
                .index[0]
            )

        else:

            mensagem_erro_principal = (
                "Nenhum erro de execução."
            )


        # ========================================================
        # MONTAR MENSAGEM
        # ========================================================

        texto_slack = (
            "*Processamento de boletos*\n\n"

            f"*Sucesso:* {percentual_sucesso:.0f}%\n"

            f"*Erros:* {total_erros} títulos\n"

            f"*Mensagem de erro principal:* "
            f"{mensagem_erro_principal}\n"
        )


        # ========================================================
        # TÍTULOS COM ERRO
        # ========================================================

        erros_df = resultado_slack[
            resultado_slack[
                "Status_Processamento"
            ] == "ERRO_EXECUCAO"
        ]


        if not erros_df.empty:

            texto_slack += (
                "\n*Detalhamento dos erros:*\n\n"
            )


            for _, linha_erro in erros_df.iterrows():

                referencia = (
                    f"{linha_erro['DocumentReferenceID_2']} "
                    f"-> "
                    f"{linha_erro['AccountingDocumentItem']}"
                )

                mensagem_erro = (
                    linha_erro["Mensagem_Erro"]
                )

                texto_slack += (
                    f"• *{referencia}* — "
                    f"{mensagem_erro}\n"
                )


        # ========================================================
        # BOLETOS GERADOS COM SUCESSO
        # ========================================================

        if boletos_gerados_slack:

            texto_slack += (
                "\n*Boletos registrados:*\n\n"
            )


            for boleto in boletos_gerados_slack:

                nome = boleto["nome"]

                cnpj = boleto["cnpj"]

                referencia = boleto["referencia"]

                url = boleto["url"]

                numero = boleto.get("numero")

                if url and str(url).startswith("http"):

                    complemento = f"<{url}|Visualizar boleto>"

                else:

                    complemento = (
                        f"Linha digitável: {boleto.get('linha_digitavel') or numero}"
                    )

                    if boleto.get("pdf_compartilhado"):

                        complemento += (
                            f" | PDF: {boleto['pdf_compartilhado']}"
                        )


                texto_slack += (
                    f"• {nome} | {cnpj} | "
                    f"{referencia} | "
                    f"{complemento}\n"
                )


        # ========================================================
        # HEADERS
        # ========================================================

        headers_slack = {

            "Authorization":
                f"Bearer {SLACK_TOKEN}",

            "Accept":
                "application/json",

            "Content-Type":
                "application/json; charset=utf-8"
        }


        # ========================================================
        # BODY
        # ========================================================

        body_slack = {

            "channel":
                SLACK_CANAL,

            "text":
                texto_slack
        }


        # ========================================================
        # ENVIO PARA O SLACK
        # ========================================================

        print()
        print("=" * 120)
        print("SLACK - RELATÓRIO FINAL")
        print("=" * 120)


        resposta_slack = requests.post(

            SLACK_URL,

            headers=headers_slack,

            json=body_slack,

            timeout=30
        )


        print(
            f"HTTP STATUS SLACK: "
            f"{resposta_slack.status_code}"
        )


        try:

            resposta_slack_json = (
                resposta_slack.json()
            )

            print(
                json.dumps(
                    resposta_slack_json,
                    indent=4,
                    ensure_ascii=False
                )
            )

        except ValueError:

            resposta_slack_json = None

            print(
                resposta_slack.text
            )


        # ========================================================
        # VALIDAR SLACK
        # ========================================================

        if (
            resposta_slack.status_code == 200
            and isinstance(
                resposta_slack_json,
                dict
            )
            and resposta_slack_json.get("ok")
        ):

            print(
                "Relatório enviado ao Slack com sucesso."
            )

        else:

            print(
                "ERRO AO ENVIAR RELATÓRIO PARA O SLACK."
            )

        # ========================================================
        # ZIP DOS PDFs (um por banco, na thread do relatório)
        # ========================================================

        try:
            from slack_zip_boletos import enviar_zip_slack
            from criar_boleto_SANTANDER import PDFS_GERADOS_NESTA_EXECUCAO as PDFS_SANTANDER
            from criar_boleto_ABC import PDFS_GERADOS_NESTA_EXECUCAO as PDFS_ABC

            thread_ts = (
                resposta_slack_json.get("ts")
                if isinstance(resposta_slack_json, dict) and resposta_slack_json.get("ok")
                else None
            )

            for banco, pdfs in (("Santander", PDFS_SANTANDER), ("ABC", PDFS_ABC)):
                if pdfs:
                    enviar_zip_slack(
                        pdfs,
                        SLACK_TOKEN,
                        SLACK_CANAL,
                        thread_ts=thread_ts,
                        prefixo=f"boletos_{banco.lower()}",
                        comentario=f"Boletos {banco} desta execução: {len(pdfs)} PDF(s).",
                    )

        except Exception as erro_zip:
            print(f"AVISO: zip dos boletos não enviado: {erro_zip}")
# ============================================================
# AJUSTE DE PARCELAS DE CLIENTES (ajustar_parcelas_clientes.py)
#
# Roda depois do processamento de boletos, no mesmo disparo do
# api_cobranca.timer. Executa em processo separado: um erro no
# ajuste não afeta o que o processador já fez, e vice-versa.
#
# Parâmetros (parametros_prod.txt), todos opcionais:
#   AJUSTE_PARCELAS_ATIVO=SIM        NAO desliga o ajuste
#   AJUSTE_PARCELAS_A_PARTIR=17:59   só roda a partir desse horário
#                                    (o timer dispara a cada 30 min; com 19:00
#                                    o ajuste roda 1x por dia, no último disparo).
#                                    00:00 = roda em todos os disparos.
#   AJUSTE_PARCELAS_PARAMETROS=parametros_ajuste_PROD.txt
# ============================================================

import subprocess
import sys

print()
print("=" * 120)
print("AJUSTE DE PARCELAS DE CLIENTES")
print("=" * 120)

AJUSTE_ATIVO = parametros.get("AJUSTE_PARCELAS_ATIVO", "SIM").strip().upper() in ("SIM", "S", "1", "TRUE")
AJUSTE_A_PARTIR = parametros.get("AJUSTE_PARCELAS_A_PARTIR", "17:59").strip()
AJUSTE_PARAMETROS = parametros.get("AJUSTE_PARCELAS_PARAMETROS", "parametros_prod.txt").strip()

try:
    hora_min = datetime.strptime(AJUSTE_A_PARTIR, "%H:%M").time()
except ValueError:
    print(f"AJUSTE_PARCELAS_A_PARTIR inválido ('{AJUSTE_A_PARTIR}'); usando 17:59.")
    hora_min = datetime.strptime("17:59", "%H:%M").time()

agora = datetime.now()
pasta_script = os.path.dirname(os.path.abspath(__file__))
script_ajuste = os.path.join(pasta_script, "ajustar_parcelas_clientes.py")

if not AJUSTE_ATIVO:
    print("Ajuste desativado (AJUSTE_PARCELAS_ATIVO=NAO).")

elif agora.time() < hora_min:
    print(f"Ajuste roda a partir das {hora_min:%H:%M}. Agora: {agora:%H:%M}. Pulando neste disparo.")

elif not os.path.exists(script_ajuste):
    print(f"ERRO: {script_ajuste} não encontrado. Ajuste não executado.")

else:
    try:
        # sys.stdout.flush: garante a ordem do log no journalctl
        sys.stdout.flush()
        retorno = subprocess.run(
            [sys.executable, script_ajuste, "--parametros", AJUSTE_PARAMETROS],
            cwd=pasta_script,
            env={**os.environ, "PYTHONIOENCODING": "utf-8"},
            timeout=2 * 60 * 60
        )
        print(f"Ajuste de parcelas finalizado (código de saída {retorno.returncode}).")

    except subprocess.TimeoutExpired:
        print("ERRO: ajuste de parcelas excedeu 2 horas e foi interrompido.")

    except Exception as erro:
        print(f"ERRO ao executar o ajuste de parcelas: {erro}")