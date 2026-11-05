"""
ajustar_parcelas_clientes.py -- Ajusta automaticamente as parcelas de títulos de CLIENTE
conforme as duplicatas do XML da NF anexado no Pipefy. Um único script, sem arquivos
intermediários e sem confirmações.

Fluxo
  1) SAP (YY1_API_ITENS_CLIENTE): partidas em aberto RV, BRL, chave 01, cliente preenchido;
     candidatos = documentos com alguma partida vencendo na data de lançamento (DELTA = 0).
  2) Nº da NF = DocumentReferenceID_2 sem zeros à esquerda e sem o "-" em diante.
  3) Pipefy (pipe 304239821): card pelo nº da NF; XML do campo xml_da_nota (link assinado
     dos anexos do card). Confere nº da NF e empresa emitente (CNPJ -> CompanyCode).
  4) Compara partidas do SAP x cobr/dup do XML.
  5) Se diferente (e mesmo total), REPARCELA no SAP:
       A) Journal Entry Post (síncrono): item 001 = contrapartida (total, crédito);
          itens 002.. = duplicatas do XML (vencimento = dVenc, 0 dias).
          Data de lançamento, data do documento e referência = as do título ORIGINAL.
       B) Journal Entry Clearing (WS-RM): compensa as partidas antigas x item 001 do novo.
       C) Confirma a compensação relendo o SAP (até POLL_TENTATIVAS x POLL_INTERVALO_S).
          Se não confirmar, ESTORNA o documento novo (nada fica em dobro). Se o estorno for
          recusado porque a compensação entrou atrasada, conta como ajustado.
  6) Conciliação com adiantamento pago (DZ, chave 19, em aberto, mais recente primeiro;
     chave empresa + cliente + valor com tolerância de R$ 0,01):
       * título AJUSTADO cuja parcela nova vence na data de lançamento -> concilia essa parcela;
       * título NÃO ajustado (sem card, XML divergente etc.) -> concilia a partida que vence na
         data de lançamento; se nenhuma bater, o título inteiro.
     Compensação com data de HOJE, confirmada relendo o SAP. Cada adiantamento é usado uma vez.
  7) Busca reversa (desliga com --sem-pagamentos): cada pagamento recebido em aberto (documento
     140..., DZ chave 19) que sobrou do passo 6 procura uma partida em aberto (qualquer RV chave 01
     do mesmo cliente, fora as que o passo 5/6 já alterou) com a mesma empresa + cliente + valor.
     Achou -> concilia; com várias, usa a de vencimento mais antigo. Não achar é silencioso: só os
     casos encontrados aparecem no resumo, no Slack e no titulos_ajustados.csv.

Não mexe: títulos já iguais ao XML, NF sem duplicatas, total diferente, razão especial,
NF não encontrada/ambígua sem adiantamento correspondente -- aparecem no resumo para análise.

Uso:
  python ajustar_parcelas_clientes.py                 executa
  python ajustar_parcelas_clientes.py --simular       só mostra o que faria (não lança)
  python ajustar_parcelas_clientes.py --empresa BR01 --limite 5
  python ajustar_parcelas_clientes.py --doc 9403124488
  python ajustar_parcelas_clientes.py --parametros C:\\caminho\\parametros_prod.txt

Parâmetros (parametros_prod.txt, mesmo arquivo do processador de boletos):
  SAP_1_URL / SAP_1_USUARIO / SAP_1_SENHA        YY1_API_ITENS_CLIENTE (leitura dos títulos)
  SAP_2_URL / SAP_2_USUARIO / SAP_2_SENHA        API_BUSINESS_PARTNER (CNPJ/CPF do cliente)
  SAP_SOAP_URL / SAP_SOAP_USUARIO / SAP_SOAP_SENHA
                                                 SAP_COM_0002 (lançamento e compensação;
                                                 o host do SAP_SOAP_URL define o ambiente)
  PIPEFY_TOKEN                                   token da API do Pipefy
  SLACK_URL / SLACK_CANAL / SLACK_TOKEN          relatório no Slack (se ausentes, não envia)
  ARQUIVO_TITULOS_AJUSTADOS                      opcional; padrão: titulos_ajustados.csv ao lado
                                                 do script (data,ajustados,conciliados -- lido pelo
                                                 dashboard, uma linha do gráfico para cada coluna)

Reporte (só em execução real, não no --simular):
  * Slack: títulos ajustados (original -> documento novo e parcelas), erros da execução e
    contagem dos casos que pedem análise manual.
  * titulos_ajustados.csv: soma, por dia, os títulos reparcelados (ajustados) e as compensações
    com adiantamento/pagamento (conciliados), em colunas separadas (linha do dia é atualizada se
    o script rodar mais de uma vez no mesmo dia).
  * titulos_conciliados_adiantamento.csv (data,Chave_Titulo,adiantamento,valor): uma linha por
    item de título baixado com adiantamento/pagamento -- o dashboard mostra esses itens como
    "Outras formas de pagamento". Caminho opcional: ARQUIVO_TITULOS_CONCILIADOS_ADT.
"""
import argparse
import csv
import os
import re
import sys
import time
import uuid
from collections import defaultdict
from datetime import date, datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from urllib.parse import urlsplit

import requests
from lxml import etree

# ======================================================================
# CONFIGURAÇÃO
# ======================================================================
PIPE_ID = "304239821"
CAMPOS_NUMERO_NF = ["n_mero_da_nota_fiscal", "copy_of_n_mero_da_nota_fiscal"]
CAMPO_XML = "xml_da_nota"

TIPO_DOC_NOVAS = "RV"          # documento com as parcelas novas
TIPO_DOC_COMPENSACAO = "AB"    # documento de compensação
ITEM_CONTRAPARTIDA = "001"     # item do documento novo que é abatido
TOLERANCIA = Decimal("0.01")
# Confirmação pós-compensação (ela é assíncrona no SAP -- "enviou sem erro" não é
# garantia de "foi processada") e estorno automático se não confirmar, pra não deixar
# parcelas em dobro. Mesmo mecanismo do ajuste_parcelas.py (Contas a Pagar).
POLL_TENTATIVAS = 10
POLL_INTERVALO_S = 6
ESTORNAR_SE_NAO_COMPENSAR = True
MOTIVO_ESTORNO = "02"  # estorno com data de lançamento diferente da original
# ======================================================================

PIPEFY_API_URL = "https://api.pipefy.com/graphql"


def carregar_parametros(arquivo):
    """Mesmo formato do processador/dashboard: CHAVE=valor, # comenta."""
    parametros = {}
    if not os.path.exists(arquivo):
        sys.exit(f"Arquivo de parâmetros não encontrado: {arquivo}")
    with open(arquivo, "r", encoding="utf-8") as f:
        for linha in f:
            linha = linha.strip()
            if not linha or linha.startswith("#") or "=" not in linha:
                continue
            chave, valor = linha.split("=", 1)
            parametros[chave.strip()] = valor.strip().strip('"').strip("'")
    return parametros


def _arquivo_parametros():
    """--parametros CAMINHO na linha de comando; senão parametros_prod.txt ao lado do script."""
    if "--parametros" in sys.argv:
        i = sys.argv.index("--parametros")
        if i + 1 < len(sys.argv):
            return sys.argv[i + 1]
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "parametros_prod.txt")


PARAMETROS = carregar_parametros(_arquivo_parametros())


def _param(chave):
    valor = PARAMETROS.get(chave, "")
    if not valor:
        sys.exit(f"Parâmetro {chave} ausente no arquivo de parâmetros.")
    return valor


URL_CDS = _param("SAP_1_URL").rstrip("/")
SAP_USER, SAP_PASS = _param("SAP_1_USUARIO"), _param("SAP_1_SENHA")
URL_BP = _param("SAP_2_URL").rstrip("/")
BP_USER, BP_PASS = _param("SAP_2_USUARIO"), _param("SAP_2_SENHA")
JE_USER, JE_PASS = _param("SAP_SOAP_USUARIO"), _param("SAP_SOAP_SENHA")
PIPEFY_TOKEN = _param("PIPEFY_TOKEN")

# Ambiente dos lançamentos = host do SAP_SOAP_URL
_soap = urlsplit(_param("SAP_SOAP_URL"))
BASE_URL = f"{_soap.scheme}://{_soap.netloc}"

FILTRO_SAP = ("AccountingDocumentType eq 'RV' and Customer ne '' "
              "and ClearingJournalEntry eq '' and TransactionCurrency eq 'BRL' "
              "and PostingKey eq '01' and (PaymentMethod eq '' or PaymentMethod eq 'E')")
CAMPOS_SAP = ["CompanyCode", "AccountingDocument", "FiscalYear", "AccountingDocumentItem",
              "DocumentReferenceID_2", "Dt_lcto", "NetDueDate",
              "AmountInCompanyCodeCurrency", "Customer"]
PAGINA_SAP = 5000

# Adiantamentos pagos em aberto (DZ, chave 19) -- usados pra conciliar automaticamente
# títulos que continuam com lançamento == vencimento mesmo depois do ajuste por NF/XML.
FILTRO_ADIANTAMENTO = ("TransactionCurrency eq 'BRL' and ClearingDate eq null "
                       "and AccountingDocumentType eq 'DZ' and Customer ne '' and PostingKey eq '19'")
# Busca reversa: pagamentos recebidos em aberto (documentos 140...) que sobraram da
# conciliação por título são casados com QUALQUER partida em aberto do mesmo cliente.
PREFIXO_PAGAMENTO = "140"
CAMPOS_ADIANTAMENTO = ["CompanyCode", "AccountingDocument", "FiscalYear", "AccountingDocumentItem",
                       "AmountInCompanyCodeCurrency", "Customer", "PostingDate"]

EP_POST = f"{BASE_URL}/sap/bc/srt/scs_ext/sap/journalentrycreaterequestconfi"
EP_CLEAR = f"{BASE_URL}/sap/bc/srt/scs_ext/sap/journalentrybulkclearingreques"
ACT_POST = ("http://sap.com/xi/SAPSCORE/SFIN/JournalEntryCreateRequestConfirmation_In/"
            "JournalEntryCreateRequestConfirmation_InRequest")
ACT_CLEAR = ("http://sap.com/xi/SAPSCORE/SFIN/JournalEntryBulkClearingRequest_In/"
             "JournalEntryBulkClearingRequest_InRequest")

_DIR = os.path.dirname(os.path.abspath(__file__))
WSDL_POST = os.path.join(_DIR, "apis_sap", "JOURNALENTRYCREATEREQUESTCONFI.wsdl")
WSDL_CLEAR = os.path.join(_DIR, "apis_sap", "JOURNALENTRYBULKCLEARINGREQUES.wsdl")

SOAP_NS = "http://schemas.xmlsoap.org/soap/envelope/"
WSA_NS = "http://www.w3.org/2005/08/addressing"
WSRM_NS = "http://docs.oasis-open.org/ws-rx/wsrm/200702"
SFIN_NS = "http://sap.com/xi/SAPSCORE/SFIN"
XS_NS = "http://www.w3.org/2001/XMLSchema"
NSMAP = {"soap-env": SOAP_NS, "wsa": WSA_NS, "wsrm": WSRM_NS}
ANONYMOUS = f"{WSA_NS}/anonymous"

def ambiente():
    if "myXXXXXX" in BASE_URL:
        return "QAS"
    if "myXXXXXX" in BASE_URL:
        return "PRODUÇÃO"
    return BASE_URL


CENTAVO = Decimal("0.01")
NS_NFE = "http://www.portalfiscal.inf.br/nfe"




# ----------------------------------------------------------------------
# Utilidades
# ----------------------------------------------------------------------
def _dec(v) -> Decimal:
    return Decimal(str(v).strip()).quantize(CENTAVO, ROUND_HALF_UP)


def _data(valor):
    """Aceita /Date(ms)/ do OData V2, ISO (aaaa-mm-dd...) e dd.mm.aaaa."""
    if valor is None or valor == "":
        return None
    if isinstance(valor, date):
        return valor
    s = str(valor)
    m = re.search(r"/Date\((-?\d+)", s)
    if m:
        return datetime.fromtimestamp(int(m.group(1)) / 1000, tz=timezone.utc).date()
    for fmt, n in (("%Y-%m-%d", 10), ("%d.%m.%Y", 10), ("%d/%m/%Y", 10)):
        try:
            return datetime.strptime(s[:n], fmt).date()
        except ValueError:
            pass
    return None


def numero_nf(document_reference: str) -> str:
    """'00002534-1' -> '2534' ; '001234567-8' -> '40788'."""
    base = str(document_reference or "").split("-", 1)[0].strip()
    base = re.sub(r"\D", "", base)
    return base.lstrip("0") or ("0" if base else "")


# ----------------------------------------------------------------------
# 1) SAP
# ----------------------------------------------------------------------
TEM_RAZAO_ESPECIAL = True


def ler_partidas_sap(empresa=None, documento=None):
    global TEM_RAZAO_ESPECIAL
    filtro = FILTRO_SAP
    if empresa:
        filtro += f" and CompanyCode eq '{empresa}'"
    if documento:
        filtro += f" and AccountingDocument eq '{documento}'"

    s = requests.Session()
    s.auth = (SAP_USER, SAP_PASS)
    campos = CAMPOS_SAP
    linhas, skip = [], 0
    while True:
        r = s.get(URL_CDS, params={"$filter": filtro, "$select": ",".join(campos),
                                   "$format": "json", "$top": PAGINA_SAP, "$skip": skip},
                  headers={"Accept": "application/json"}, timeout=120)
        if not r.ok:
            sys.exit(f"SAP respondeu HTTP {r.status_code} na YY1_API_ITENS_CLIENTE:\n{r.text[:1000]}")
        pagina = r.json().get("d", {}).get("results", [])
        linhas.extend(pagina)
        if len(pagina) < PAGINA_SAP:
            return linhas
        skip += PAGINA_SAP


CAMPOS_CONFIRMACAO = ["CompanyCode", "AccountingDocument", "FiscalYear", "AccountingDocumentItem",
                      "AmountInCompanyCodeCurrency", "Customer", "ClearingJournalEntry", "NetDueDate"]


def ler_linhas_documento(empresa, documento, ano):
    """Relê TODAS as linhas de UM documento específico (sem filtro de RV/chave/aberto --
    o documento pode já estar compensado, é exatamente isso que queremos conferir).
    Usada só pra confirmação pós-compensação (polling), nunca pra achar candidatos."""
    filtro = f"CompanyCode eq '{empresa}' and AccountingDocument eq '{documento}' and FiscalYear eq '{ano}'"
    s = requests.Session()
    s.auth = (SAP_USER, SAP_PASS)
    r = s.get(URL_CDS, params={"$filter": filtro, "$select": ",".join(CAMPOS_CONFIRMACAO),
                               "$format": "json", "$top": PAGINA_SAP},
              headers={"Accept": "application/json"}, timeout=60)
    if not r.ok:
        return []
    return r.json().get("d", {}).get("results", [])


def _aguardar_compensacao(empresa, itens_esperados, out=print):
    """itens_esperados: [(documento, ano, item), ...] -- pode misturar documentos
    diferentes (ex.: parcelas antigas num documento + contrapartida no documento novo).
    Espera até POLL_TENTATIVAS*POLL_INTERVALO_S segundos que TODOS apareçam com
    ClearingJournalEntry preenchido. Devolve True se confirmou, False se não."""
    docs = {(d, a) for d, a, _ in itens_esperados}
    for i in range(1, POLL_TENTATIVAS + 1):
        time.sleep(POLL_INTERVALO_S)
        linhas_por_doc = {da: ler_linhas_documento(empresa, da[0], da[1]) for da in docs}
        pendentes = []
        for documento, ano, item in itens_esperados:
            por_item = {str(l.get("AccountingDocumentItem")).zfill(3): l
                       for l in linhas_por_doc.get((documento, ano), [])}
            l = por_item.get(str(item).zfill(3))
            if not l or not str(l.get("ClearingJournalEntry") or "").strip():
                pendentes.append((documento, item))
        if not pendentes:
            return True
        out(f"      tentativa {i}/{POLL_TENTATIVAS}: aguardando compensação confirmar ({len(pendentes)} item(ns) pendente(s))")
    return False


def montar_estorno(documento, ano, empresa, message_id, motivo=MOTIVO_ESTORNO, data=None):
    """Post de estorno: só cabeçalho, apontando pro lançamento original (BKPFF: chave =
    DOC+EMPRESA+ANO). O SAP gera o documento inverso e compensa os itens do original
    com ele -- usado quando a compensação do reparcelamento não confirma, pra não
    deixar o documento novo com as parcelas em dobro."""
    data = (data or date.today()).isoformat()
    env, body = _envelope()
    req = _cabecalho_msg(_sub(body, f"{{{SFIN_NS}}}JournalEntryBulkCreateRequest"),
                         message_id, "JournalEntryCreateRequest")
    je = _sub(req, "JournalEntry")
    _sub(je, "OriginalReferenceDocumentType", "BKPFF")
    _sub(je, "BusinessTransactionType", "RFBU")
    _sub(je, "AccountingDocumentType", TIPO_DOC_NOVAS)
    _sub(je, "DocumentHeaderText", f"Estorno {documento}"[:25])
    _sub(je, "ReversalReason", motivo)
    _sub(je, "ReversalReferenceDocument", f"{documento}{empresa}{ano}")
    _sub(je, "CreatedByUser", "SAP"[:12])
    _sub(je, "CompanyCode", empresa)
    _sub(je, "DocumentDate", data)
    _sub(je, "PostingDate", data)
    return env


def titulos_candidatos(linhas):
    """Agrupa por documento e devolve só os documentos com alguma partida DELTA = 0.
    Cada documento leva TODAS as suas partidas em aberto (são elas que serão comparadas)."""
    por_doc = defaultdict(list)
    for l in linhas:
        por_doc[(l["CompanyCode"], l["AccountingDocument"], str(l["FiscalYear"]))].append(l)

    candidatos = []
    for chave, itens in por_doc.items():
        tem_delta_zero = any(
            _data(i.get("NetDueDate")) is not None
            and _data(i.get("NetDueDate")) == _data(i.get("Dt_lcto"))
            for i in itens
        )
        if tem_delta_zero:
            candidatos.append((chave, sorted(itens, key=lambda i: str(i["AccountingDocumentItem"]))))
    return sorted(candidatos)


# ----------------------------------------------------------------------
# 2B) Adiantamentos pagos (conciliação automática)
# ----------------------------------------------------------------------
def ler_adiantamentos(empresa=None):
    """Adiantamentos pagos em aberto (DZ, chave 19), mais recentes primeiro --
    candidatos pra conciliar com títulos que ficaram com lançamento == vencimento."""
    filtro = FILTRO_ADIANTAMENTO
    if empresa:
        filtro += f" and CompanyCode eq '{empresa}'"

    s = requests.Session()
    s.auth = (SAP_USER, SAP_PASS)
    linhas, skip = [], 0
    while True:
        r = s.get(URL_CDS, params={"$filter": filtro, "$select": ",".join(CAMPOS_ADIANTAMENTO),
                                   "$format": "json", "$top": PAGINA_SAP, "$skip": skip,
                                   "$orderby": "PostingDate desc"},
                  headers={"Accept": "application/json"}, timeout=120)
        if not r.ok:
            print(f"[aviso] SAP respondeu HTTP {r.status_code} ao buscar adiantamentos -- seguindo SEM "
                  f"conciliação automática nesta execução:\n{r.text[:500]}")
            return []
        pagina = r.json().get("d", {}).get("results", [])
        linhas.extend(pagina)
        if len(pagina) < PAGINA_SAP:
            break
        skip += PAGINA_SAP
    # garante a ordem mesmo se o $orderby não for respeitado pelo serviço
    linhas.sort(key=lambda l: _data(l.get("PostingDate")) or date.min, reverse=True)
    return linhas


def indice_adiantamentos(linhas):
    """Chave empresa+cliente -> lista de itens (mais recente primeiro, cada um já com
    o valor em Decimal calculado). O valor não entra na chave porque a comparação
    precisa de tolerância de centavos (arredondamento é comum entre o valor do
    adiantamento e o valor do título -- ex.: R$ 5.171,90 vs R$ 5.171,91)."""
    idx = defaultdict(list)
    for l in linhas:
        idx[(l["CompanyCode"], l["Customer"])].append({**l, "_valor": abs(_dec(l["AmountInCompanyCodeCurrency"]))})
    return idx


def buscar_adiantamento(idx, empresa, cliente, valor):
    """Devolve o adiantamento mais recente (empresa+cliente, valor dentro de
    TOLERANCIA) e já o remove do índice, pra não ser usado de novo noutro título
    na mesma execução."""
    candidatos = idx.get((empresa, cliente)) or []
    for i, c in enumerate(candidatos):
        if abs(c["_valor"] - valor) <= TOLERANCIA:
            return candidatos.pop(i)
    return None


# ----------------------------------------------------------------------
# 3) Pipefy
# ----------------------------------------------------------------------
_QUERY_FIND = """
query Buscar($pipe: ID!, $campo: String!, $valor: String!, $after: String) {
  findCards(pipeId: $pipe, search: {fieldId: $campo, fieldValue: $valor}, first: 50, after: $after) {
    pageInfo { hasNextPage endCursor }
    edges {
      node {
        id
        title
        current_phase { name }
        attachments { url path }
        fields { field { id } value array_value }
      }
    }
  }
}
"""

_HEADERS_PIPEFY = {"Authorization": f"Bearer {PIPEFY_TOKEN}", "Content-Type": "application/json"}


def buscar_cards(nf: str):
    """Procura nos dois campos de nº da NF. Tenta o número limpo e com zeros (9 dígitos)."""
    encontrados = {}
    for campo in CAMPOS_NUMERO_NF:
        for valor in dict.fromkeys([nf, nf.zfill(9)]):
            after = None
            while True:
                r = requests.post(PIPEFY_API_URL, headers=_HEADERS_PIPEFY, timeout=30, json={
                    "query": _QUERY_FIND,
                    "variables": {"pipe": PIPE_ID, "campo": campo, "valor": valor, "after": after}})
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
    return list(encontrados.values())


def _valores_campo_xml(card):
    """Valores brutos do campo xml_da_nota (caminhos ou URLs)."""
    valores = []
    for f in card.get("fields", []):
        if f["field"]["id"] != CAMPO_XML:
            continue
        valores += [str(v) for v in (f.get("array_value") or [])]
        bruto = str(f.get("value") or "")
        if bruto and not valores:
            # value costuma vir como texto de lista: ["orgs/.../NF.xml"]
            valores += re.findall(r'[^\s"\[\],]+\.(?:xml|zip)[^\s"\[\],]*', bruto, re.I) or [bruto]
    return [v.strip() for v in valores if v.strip()]


def _caminho(v):
    """Parte do caminho que identifica o arquivo, sem domínio e sem querystring."""
    v = re.sub(r"^https?://[^/]+/", "", v.split("?", 1)[0])
    return v.lstrip("/")


def urls_xml(card):
    """Links baixáveis do XML do card.
    O campo xml_da_nota guarda o CAMINHO do arquivo; o link assinado (baixável) vem
    em card.attachments. Casa os dois pelo caminho; se não casar, usa o próprio valor."""
    anexos = card.get("attachments") or []
    urls = []
    for valor in _valores_campo_xml(card):
        alvo = _caminho(valor)
        nome = alvo.rsplit("/", 1)[-1]
        casados = [a["url"] for a in anexos if a.get("url") and (
            _caminho(a.get("path") or "") == alvo
            or _caminho(a.get("url") or "") == alvo
            or (nome and (a.get("path") or "").endswith(nome)))]
        if casados:
            urls += casados
        elif valor.startswith("http"):
            urls.append(valor)
        else:
            urls.append("https://app.pipefy.com/" + valor.lstrip("/"))
    # Campo vazio mas card com XML anexado em outro lugar: aproveita
    if not urls:
        urls = [a["url"] for a in anexos
                if a.get("url") and re.search(r"\.(xml|zip)(\?|$)", (a.get("path") or a["url"]), re.I)]
    return list(dict.fromkeys(urls))


def _extrair_xml(conteudo: bytes) -> bytes:
    """Aceita XML puro (com ou sem BOM) ou ZIP com um .xml dentro."""
    c = conteudo.lstrip(b"\xef\xbb\xbf").lstrip()
    if c.startswith(b"<"):
        return c
    if c.startswith(b"PK"):
        import io, zipfile
        with zipfile.ZipFile(io.BytesIO(conteudo)) as z:
            for nome in z.namelist():
                if nome.lower().endswith(".xml"):
                    return z.read(nome).lstrip(b"\xef\xbb\xbf").lstrip()
    return b""


def baixar_xml(url):
    """Tenta sem header (link pré-assinado) e depois com o token do Pipefy.
    Em caso de falha, devolve o motivo de cada tentativa."""
    motivos = []
    for rotulo, headers in (("sem token", None),
                            ("com token", {"Authorization": _HEADERS_PIPEFY["Authorization"]})):
        try:
            r = requests.get(url, headers=headers, timeout=60)
            if r.ok:
                xml = _extrair_xml(r.content)
                if xml:
                    return xml
                motivos.append(f"{rotulo}: HTTP 200 mas não é XML "
                               f"({r.headers.get('Content-Type', '?')}, início {r.content[:15]!r})")
            else:
                motivos.append(f"{rotulo}: HTTP {r.status_code}")
        except requests.RequestException as e:
            motivos.append(f"{rotulo}: {e.__class__.__name__}")
    raise RuntimeError("não consegui baixar o XML -> " + " | ".join(motivos)
                       + f" | url: {url[:120]}")


# ----------------------------------------------------------------------
# 4) XML da NF-e
# ----------------------------------------------------------------------
def _t(el, caminho):
    achado = el.find(caminho, {"n": NS_NFE})
    return achado.text.strip() if achado is not None and achado.text else ""


def ler_nfe(conteudo: bytes) -> dict:
    root = etree.fromstring(conteudo)
    inf = root.find(".//n:infNFe", {"n": NS_NFE})
    if inf is None:
        raise ValueError("XML não é uma NF-e (infNFe não encontrado)")
    dups = [{
        "nDup": _t(d, "n:nDup"),
        "dVenc": _data(_t(d, "n:dVenc")),
        "vDup": _dec(_t(d, "n:vDup") or "0"),
    } for d in inf.findall("n:cobr/n:dup", {"n": NS_NFE})]
    return {
        "nNF": _t(inf, "n:ide/n:nNF"),
        "serie": _t(inf, "n:ide/n:serie"),
        "emissao": _data(_t(inf, "n:ide/n:dhEmi") or _t(inf, "n:ide/n:dEmi")),
        "cnpj_emit": _t(inf, "n:emit/n:CNPJ"),
        "doc_dest": _t(inf, "n:dest/n:CNPJ") or _t(inf, "n:dest/n:CPF"),
        "nome_dest": _t(inf, "n:dest/n:xNome"),
        "cStat": _t(root, ".//n:protNFe/n:infProt/n:cStat"),
        "fatura": {
            "nFat": _t(inf, "n:cobr/n:fat/n:nFat"),
            "vOrig": _t(inf, "n:cobr/n:fat/n:vOrig"),
            "vLiq": _t(inf, "n:cobr/n:fat/n:vLiq"),
        },
        "duplicatas": sorted(dups, key=lambda d: (d["dVenc"] or date.max, d["nDup"])),
    }


_cache_docs_cliente = {}


def documentos_cliente(cliente):
    """CNPJ/CPF (BR1/BR2) do cliente no SAP -- API_BUSINESS_PARTNER (SAP_2)."""
    if cliente not in _cache_docs_cliente:
        r = requests.get(f"{URL_BP}/A_BusinessPartnerTaxNumber", auth=(BP_USER, BP_PASS), timeout=60,
                         headers={"Accept": "application/json"},
                         params={"$filter": f"BusinessPartner eq '{cliente}' and "
                                            f"(BPTaxType eq 'BR1' or BPTaxType eq 'BR2')",
                                 "$select": "BPTaxNumber", "$format": "json"})
        r.raise_for_status()
        _cache_docs_cliente[cliente] = {
            re.sub(r"\D", "", x.get("BPTaxNumber") or "")
            for x in r.json().get("d", {}).get("results", [])} - {""}
    return _cache_docs_cliente[cliente]


def xml_do_titulo(nf, cliente, cards):
    """Entre os cards encontrados, devolve (card, nfe) cujo XML é desta NF e deste cliente
    (CNPJ/CPF do destinatário = CNPJ/CPF do cliente no SAP)."""
    docs = documentos_cliente(cliente)
    if not docs:
        return [], [f"cliente {cliente} sem CNPJ/CPF (BR1/BR2) no SAP para conferir o XML"]
    validos, problemas = [], []
    for card in cards:
        urls = urls_xml(card)
        if not urls:
            problemas.append(f"card {card['id']}: sem XML no campo {CAMPO_XML}")
            continue
        for url in urls:
            try:
                nfe = ler_nfe(baixar_xml(url))
            except Exception as e:
                problemas.append(f"card {card['id']}: {e}")
                continue
            if nfe["nNF"].lstrip("0") != nf:
                problemas.append(f"card {card['id']}: XML é da NF {nfe['nNF']}, não da {nf}")
                continue
            if re.sub(r"\D", "", nfe["doc_dest"]) not in docs:
                problemas.append(f"card {card['id']}: destinatário {nfe['doc_dest']} não é o cliente {cliente}")
                continue
            validos.append((card, nfe))
    return validos, problemas


# ----------------------------------------------------------------------
# 5) Comparação
# ----------------------------------------------------------------------
def comparar(itens_sap, nfe):
    dups = nfe["duplicatas"]
    if not dups:
        return "SEM_DUPLICATAS", "XML sem cobr/dup (NF à vista): nada a ajustar."

    total_sap = sum((abs(_dec(i["AmountInCompanyCodeCurrency"])) for i in itens_sap), Decimal("0"))
    total_xml = sum((d["vDup"] for d in dups), Decimal("0"))
    if abs(total_sap - total_xml) > TOLERANCIA:
        return ("TOTAL_DIFERENTE",
                f"Em aberto no SAP R$ {total_sap} ≠ duplicatas do XML R$ {total_xml} "
                f"(pode haver pagamento parcial ou NF diferente). Análise manual.")

    sap_ord = sorted(itens_sap, key=lambda i: (_data(i.get("NetDueDate")) or date.max,
                                               str(i["AccountingDocumentItem"])))
    iguais = len(sap_ord) == len(dups) and all(
        abs(abs(_dec(i["AmountInCompanyCodeCurrency"])) - d["vDup"]) <= TOLERANCIA
        and _data(i.get("NetDueDate")) == d["dVenc"]
        for i, d in zip(sap_ord, dups))
    if iguais:
        return "OK", "Parcelas do SAP já iguais às duplicatas do XML."

    return "AJUSTAR", (f"SAP: {len(sap_ord)} parcela(s) | XML: {len(dups)} duplicata(s) | "
                       f"total R$ {total_xml}")



# ----------------------------------------------------------------------
# 5) Reparcelamento: monta a entrada a partir do SAP + XML
# ----------------------------------------------------------------------
def montar_entrada(chave, itens_sap, nfe, nf):
    empresa, _, _ = chave
    data_original = _data(itens_sap[0].get("Dt_lcto"))
    if data_original is None:
        raise ValueError("título sem data de lançamento (Dt_lcto)")
    referencia = str(itens_sap[0].get("DocumentReferenceID_2") or "").strip() or f"NF {nf}"
    antigas = [{"doc": chave[1], "ano": chave[2], "item": str(i["AccountingDocumentItem"]).zfill(3),
                "valor": abs(_dec(i["AmountInCompanyCodeCurrency"]))} for i in itens_sap]
    n = len(nfe["duplicatas"])
    novas = [{"valor": d["vDup"], "venc": d["dVenc"], "texto": f"Parcela {k}/{n} NF {nf}"}
             for k, d in enumerate(nfe["duplicatas"], start=1)]
    if any(p["venc"] is None for p in novas):
        raise ValueError("duplicata sem data de vencimento no XML")

    # O documento novo tem contrapartida = total em aberto no SAP e parcelas = XML. Se os
    # dois totais diferem por arredondamento (comparar() aceita até TOLERANCIA), o documento
    # não fecha e o SAP recusa ("Balance in Transaction Currency 0.01-"). O centavo vai pra
    # ÚLTIMA parcela -- a 1ª costuma ser a casada com adiantamento pelo valor do XML.
    total = sum((p["valor"] for p in antigas), Decimal("0"))
    ajuste_centavos = total - sum((p["valor"] for p in novas), Decimal("0"))
    if ajuste_centavos:
        if abs(ajuste_centavos) > TOLERANCIA:
            raise ValueError(f"total do SAP {total} difere das duplicatas além da tolerância")
        novas[-1]["valor"] += ajuste_centavos
        if novas[-1]["valor"] <= 0:
            raise ValueError("ajuste de arredondamento deixaria a última parcela sem valor")

    return {
        "cab": {"EMPRESA": empresa, "TIPO_CONTA": "D", "PARCEIRO": itens_sap[0]["Customer"],
                "MOEDA": "BRL", "_LANC": data_original, "_DOC": data_original,
                "TIPO_DOC_NOVAS": TIPO_DOC_NOVAS, "TIPO_DOC_COMPENSACAO": TIPO_DOC_COMPENSACAO,
                "REFERENCIA": referencia, "TEXTO_CABECALHO": f"Ajuste parcelas NF {nf}",
                "CRIADO_POR": "SAP", "DIAS_LIQUIDOS": "0"},
        "antigas": antigas,
        "novas": novas,
        "total": total,
        "ajuste_centavos": ajuste_centavos,
    }


def _agora():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _sub(pai, tag, texto=None, **attrs):
    el = etree.SubElement(pai, tag, **attrs)
    if texto is not None:
        el.text = str(texto)
    return el


def _envelope():
    env = etree.Element(f"{{{SOAP_NS}}}Envelope", nsmap={**NSMAP, "sfin": SFIN_NS})
    etree.SubElement(env, f"{{{SOAP_NS}}}Header")
    return env, etree.SubElement(env, f"{{{SOAP_NS}}}Body")


def _cabecalho_msg(raiz, msg_id, filho):
    mh = _sub(raiz, "MessageHeader")
    _sub(mh, "ID", msg_id)
    _sub(mh, "CreationDateTime", _agora())
    req = _sub(raiz, filho)
    mh2 = _sub(req, "MessageHeader")
    _sub(mh2, "ID", f"{msg_id}_1")
    _sub(mh2, "CreationDateTime", _agora())
    return req


def montar_post(e, msg_id):
    c = e["cab"]
    cliente = c["TIPO_CONTA"] == "D"
    tag_item, tag_parc = ("DebtorItem", "Debtor") if cliente else ("CreditorItem", "Creditor")
    # Cliente: parcelas a débito (+), contrapartida a crédito (-). Fornecedor: o inverso.
    sinal_parc, lado_parc, lado_contra = (1, "S", "H") if cliente else (-1, "H", "S")

    env, body = _envelope()
    req = _cabecalho_msg(_sub(body, f"{{{SFIN_NS}}}JournalEntryBulkCreateRequest"),
                         msg_id, "JournalEntryCreateRequest")
    je = _sub(req, "JournalEntry")
    _sub(je, "OriginalReferenceDocumentType", "BKPFF")
    _sub(je, "BusinessTransactionType", "RFBU")
    _sub(je, "AccountingDocumentType", c["TIPO_DOC_NOVAS"])
    if c.get("REFERENCIA"):
        _sub(je, "DocumentReferenceID", c["REFERENCIA"][:16])
    if c.get("TEXTO_CABECALHO"):
        _sub(je, "DocumentHeaderText", c["TEXTO_CABECALHO"][:25])
    _sub(je, "CreatedByUser", (c.get("CRIADO_POR") or "SAP")[:12])
    _sub(je, "CompanyCode", c["EMPRESA"])
    _sub(je, "DocumentDate", c["_DOC"].isoformat())
    _sub(je, "PostingDate", c["_LANC"].isoformat())

    # Item 001: contrapartida (será abatida no passo B)
    cp = _sub(je, tag_item)
    _sub(cp, "ReferenceDocumentItem", "1")
    _sub(cp, tag_parc, c["PARCEIRO"])
    _sub(cp, "AmountInTransactionCurrency", f"{-sinal_parc * e['total']:.2f}", currencyCode=c["MOEDA"])
    _sub(cp, "DebitCreditCode", lado_contra)
    _sub(cp, "DocumentItemText", "Contrapartida reparcelamento")

    # Itens 002..: parcelas novas
    for i, p in enumerate(e["novas"], start=2):
        it = _sub(je, tag_item)
        _sub(it, "ReferenceDocumentItem", str(i))
        _sub(it, tag_parc, c["PARCEIRO"])
        _sub(it, "AmountInTransactionCurrency", f"{sinal_parc * p['valor']:.2f}", currencyCode=c["MOEDA"])
        _sub(it, "DebitCreditCode", lado_parc)
        if p["texto"]:
            _sub(it, "DocumentItemText", p["texto"][:50])
        cdt = _sub(it, "CashDiscountTerms")
        _sub(cdt, "DueCalculationBaseDate", p["venc"].isoformat())
        _sub(cdt, "NetPaymentDays", f"P{int(c.get('DIAS_LIQUIDOS') or 0)}D")
    return env


def montar_clearing_itens(cab, lista_itens, msg_id):
    """lista_itens: [(doc, ano, item), ...] -- compensa todos entre si. Usada tanto no
    reparcelamento (antigas + item 001 do documento novo) quanto na conciliação direta
    com adiantamento (título x adiantamento, sem documento novo nenhum)."""
    c = cab
    env, body = _envelope()
    req = _cabecalho_msg(_sub(body, f"{{{SFIN_NS}}}JournalEntryBulkClearingRequest"),
                         msg_id, "JournalEntryClearingRequest")
    je = _sub(req, "JournalEntry")
    _sub(je, "CompanyCode", c["EMPRESA"])
    _sub(je, "AccountingDocumentType", c["TIPO_DOC_COMPENSACAO"])
    _sub(je, "DocumentDate", c["_DOC"].isoformat())
    _sub(je, "PostingDate", c["_LANC"].isoformat())
    _sub(je, "CurrencyCode", c["MOEDA"])
    if c.get("TEXTO_CABECALHO"):
        _sub(je, "DocumentHeaderText", c["TEXTO_CABECALHO"][:25])

    for ref, (doc, ano, item) in enumerate(lista_itens, start=1):
        ap = _sub(je, "APARItems")
        _sub(ap, "ReferenceDocumentItem", str(ref))
        _sub(ap, "CompanyCode", c["EMPRESA"])
        _sub(ap, "AccountType", c["TIPO_CONTA"])
        _sub(ap, "APARAccount", c["PARCEIRO"])
        _sub(ap, "FiscalYear", ano)
        _sub(ap, "AccountingDocument", doc)
        _sub(ap, "AccountingDocumentItem", item)
    return env


def montar_clearing(e, msg_id, doc_novo, ano_novo):
    itens = [(p["doc"], p["ano"], p["item"]) for p in e["antigas"]]
    itens.append((doc_novo, ano_novo, ITEM_CONTRAPARTIDA))
    return montar_clearing_itens(e["cab"], itens, msg_id)


# ----------------------------------------------------------------------
# Validação local (se os WSDLs estiverem em apis_sap\)
# ----------------------------------------------------------------------
_schemas = {}


def validar(env, wsdl):
    if wsdl not in _schemas:
        _schemas[wsdl] = None
        if os.path.exists(wsdl):
            sch = etree.parse(wsdl).getroot().find(f".//{{{XS_NS}}}schema")
            _schemas[wsdl] = etree.XMLSchema(etree.fromstring(etree.tostring(sch)))
    sch = _schemas[wsdl]
    if sch is None:
        return []
    doc = etree.fromstring(etree.tostring(env.find(f"{{{SOAP_NS}}}Body")[0]))
    return [] if sch.validate(doc) else [f"linha {x.line}: {x.message}" for x in sch.error_log]


# ----------------------------------------------------------------------
# Envio
# ----------------------------------------------------------------------
def _local(root, nome):
    return [el for el in root.iter() if isinstance(el.tag, str) and etree.QName(el).localname == nome]


def enviar_post(s, env):
    r = s.post(EP_POST, data=etree.tostring(env, xml_declaration=True, encoding="UTF-8"),
               headers={"Content-Type": "text/xml; charset=utf-8", "SOAPAction": ACT_POST}, timeout=120)
    if r.status_code in (401, 403):
        raise RuntimeError(f"HTTP {r.status_code}: usuário/senha ou permissão do SAP_COM_0002 "
                           f"em {ambiente()}")
    try:
        root = etree.fromstring(r.content)
    except etree.XMLSyntaxError:
        raise RuntimeError(f"resposta não-XML (HTTP {r.status_code}): {r.text[:300]}")
    fault = root.find(f".//{{{SOAP_NS}}}Fault")
    if fault is not None:
        raise RuntimeError(f"SOAP Fault: {fault.findtext('faultstring')}")
    notas = [n.text for n in _local(root, "Note") if n.text]
    doc = next((d.text for d in _local(root, "AccountingDocument") if d.text), "")
    ano = next((a.text for a in _local(root, "FiscalYear") if a.text), "")
    if not doc or set(doc) == {"0"}:
        raise RuntimeError("SAP recusou: " + " | ".join(notas))
    return doc, ano


def _post_rm(s, env_bytes, action):
    return s.post(EP_CLEAR, data=env_bytes, timeout=60,
                  headers={"Content-Type": "text/xml; charset=utf-8", "SOAPAction": action})


def _checar(r, etapa):
    root = None
    if r.content and r.content.strip():
        try:
            root = etree.fromstring(r.content)
        except etree.XMLSyntaxError:
            pass
    if root is not None and root.find(f".//{{{SOAP_NS}}}Fault") is not None:
        raise RuntimeError(f"{etapa}: SOAP Fault: {root.find(f'.//{{{SOAP_NS}}}Fault').findtext('faultstring')}")
    if r.status_code >= 400:
        raise RuntimeError(f"{etapa}: HTTP {r.status_code}")
    return root


def _env_protocolo(action, corpo):
    env = etree.Element(f"{{{SOAP_NS}}}Envelope", nsmap=NSMAP)
    hdr = etree.SubElement(env, f"{{{SOAP_NS}}}Header")
    etree.SubElement(hdr, f"{{{WSA_NS}}}To").text = EP_CLEAR
    etree.SubElement(hdr, f"{{{WSA_NS}}}Action").text = action
    etree.SubElement(hdr, f"{{{WSA_NS}}}MessageID").text = f"urn:uuid:{uuid.uuid4()}"
    rt = etree.SubElement(hdr, f"{{{WSA_NS}}}ReplyTo")
    etree.SubElement(rt, f"{{{WSA_NS}}}Address").text = ANONYMOUS
    etree.SubElement(env, f"{{{SOAP_NS}}}Body").append(corpo)
    return etree.tostring(env, xml_declaration=True, encoding="UTF-8")


def enviar_clearing(s, env):
    a_cs = f"{WSRM_NS}/CreateSequence"
    cs = etree.Element(f"{{{WSRM_NS}}}CreateSequence")
    etree.SubElement(etree.SubElement(cs, f"{{{WSRM_NS}}}AcksTo"), f"{{{WSA_NS}}}Address").text = ANONYMOUS
    root = _checar(_post_rm(s, _env_protocolo(a_cs, cs), a_cs), "CreateSequence")
    ident = None if root is None else root.find(".//wsrm:CreateSequenceResponse/wsrm:Identifier", NSMAP)
    if ident is None or not ident.text:
        raise RuntimeError("CreateSequence não devolveu wsrm:Identifier")
    try:
        hdr = env.find(f"{{{SOAP_NS}}}Header")
        etree.SubElement(hdr, f"{{{WSA_NS}}}To").text = EP_CLEAR
        etree.SubElement(hdr, f"{{{WSA_NS}}}Action").text = ACT_CLEAR
        etree.SubElement(hdr, f"{{{WSA_NS}}}MessageID").text = f"urn:uuid:{uuid.uuid4()}"
        rt = etree.SubElement(hdr, f"{{{WSA_NS}}}ReplyTo")
        etree.SubElement(rt, f"{{{WSA_NS}}}Address").text = ANONYMOUS
        seq = etree.SubElement(hdr, f"{{{WSRM_NS}}}Sequence")
        etree.SubElement(seq, f"{{{WSRM_NS}}}Identifier").text = ident.text
        etree.SubElement(seq, f"{{{WSRM_NS}}}MessageNumber").text = "1"
        _checar(_post_rm(s, etree.tostring(env, xml_declaration=True, encoding="UTF-8"), ACT_CLEAR),
                "Compensação")
    finally:
        a_ts = f"{WSRM_NS}/TerminateSequence"
        ts = etree.Element(f"{{{WSRM_NS}}}TerminateSequence")
        etree.SubElement(ts, f"{{{WSRM_NS}}}Identifier").text = ident.text
        try:
            _post_rm(s, _env_protocolo(a_ts, ts), a_ts)
        except Exception:
            pass



def _nota_centavos(e):
    d = e.get("ajuste_centavos") or Decimal("0")
    return f" (arredondamento de {_brl(d)} na última parcela)" if d else ""


def reparcelar(s, e, simular):
    """Executa A (Post) e B (Clearing). Devolve (status, detalhe)."""
    base = uuid.uuid4().hex[:10].upper()
    env_post = montar_post(e, f"REPARC_{base}_P")
    erros = validar(env_post, WSDL_POST) + validar(
        montar_clearing(e, "X", "9999999999", str(e["cab"]["_LANC"].year)), WSDL_CLEAR)
    if erros:
        return "ERRO", f"XML inválido, nada enviado: {erros[0]}"
    if simular:
        return "SIMULADO", (f"lançaria {len(e['novas'])} parcela(s) em {e['cab']['_LANC']:%d/%m/%Y} "
                            f"ref {e['cab']['REFERENCIA']}{_nota_centavos(e)}"), {
            "cliente": e["cab"]["PARCEIRO"], "novas": e["novas"],
            # placeholders válidos no schema (só usados na simulação da conciliação pós-ajuste)
            "doc_novo": "9999999999", "ano_novo": str(e["cab"]["_LANC"].year), "_simulado": True}
    try:
        doc_novo, ano_novo = enviar_post(s, env_post)
    except Exception as ex:
        return "RECUSADO", f"nada alterado: {ex}"

    aviso_estorno = (f"documento {doc_novo}/{ano_novo} LANÇADO: parcelas antigas E novas ficaram em aberto. "
                     f"Estorne o {doc_novo} ou compense manualmente o item 001.")
    try:
        enviar_clearing(s, montar_clearing(e, f"REPARC_{base}_C", doc_novo, ano_novo))
    except Exception as ex:
        return _falha_pos_lancamento(s, e, doc_novo, ano_novo,
                                     f"compensação recusada ({ex}); {aviso_estorno}")

    # compensação é assíncrona -- confirma de verdade antes de dizer que ajustou
    itens_esperados = [(p["doc"], p["ano"], p["item"]) for p in e["antigas"]]
    itens_esperados.append((doc_novo, ano_novo, ITEM_CONTRAPARTIDA))
    if not _aguardar_compensacao(e["cab"]["EMPRESA"], itens_esperados):
        return _falha_pos_lancamento(s, e, doc_novo, ano_novo,
                                     f"compensação não confirmada no tempo de espera; {aviso_estorno}")

    return "AJUSTADO", (f"documento {doc_novo}/{ano_novo} criado; compensação confirmada "
                        f"(Message Dashboard ID REPARC_{base}_C){_nota_centavos(e)}"), {
        "doc_novo": doc_novo, "ano_novo": ano_novo, "total": e["total"],
        "cliente": e["cab"]["PARCEIRO"], "antigas": e["antigas"], "novas": e["novas"]}


def _contrapartida_compensada(empresa, doc_novo, ano_novo):
    """Item 001 (contrapartida) do documento novo já tem documento de compensação?"""
    for l in ler_linhas_documento(empresa, doc_novo, ano_novo):
        if str(l.get("AccountingDocumentItem")).zfill(3) == ITEM_CONTRAPARTIDA:
            return str(l.get("ClearingJournalEntry") or "").strip()
    return ""


def _falha_pos_lancamento(s, e, doc_novo, ano_novo, motivo):
    """A compensação do reparcelamento falhou ou não confirmou com o documento novo já
    lançado. Pra não deixar parcelas em dobro, estorna automaticamente o documento novo
    (volta a ser como estava antes, pra análise manual) -- mesmo mecanismo do
    ajuste_parcelas.py (Contas a Pagar). Se o estorno for recusado porque a compensação
    entrou atrasada (depois do tempo de espera), conta como AJUSTADO."""
    empresa = e["cab"]["EMPRESA"]
    info_ok = {"doc_novo": doc_novo, "ano_novo": ano_novo, "total": e["total"],
               "cliente": e["cab"]["PARCEIRO"], "antigas": e["antigas"], "novas": e["novas"]}
    if not ESTORNAR_SE_NAO_COMPENSAR:
        return "COMPENSACAO_FALHOU", motivo, {"doc_novo": doc_novo, "ano_novo": ano_novo}
    try:
        env = montar_estorno(doc_novo, ano_novo, empresa, f"AJEST_{uuid.uuid4().hex[:10].upper()}")
        doc_est, _ = enviar_post(s, env)
    except Exception as ex:
        comp = _contrapartida_compensada(empresa, doc_novo, ano_novo)
        if comp and comp != doc_novo:
            return "AJUSTADO", (f"documento {doc_novo}/{ano_novo} criado; a compensação entrou atrasada "
                                f"(documento {comp}), nada a estornar"), info_ok
        return "COMPENSACAO_FALHOU", (f"{motivo} | O estorno automático também falhou ({ex}): "
                                      f"estorne o {doc_novo}/{ano_novo} manualmente."), {
            "doc_novo": doc_novo, "ano_novo": ano_novo}
    return "COMPENSACAO_FALHOU", (f"{motivo} | Documento {doc_novo}/{ano_novo} estornado automaticamente "
                                  f"(estorno {doc_est}): parcelas voltam a ser como no título original, "
                                  f"sem nada em dobro. Análise manual do motivo da falha."), {
        "doc_novo": doc_novo, "ano_novo": ano_novo, "estorno": doc_est}


def conciliar_adiantamento(s, chave, itens_titulo, adiantamento, simular):
    """Compensa diretamente o título (todos os itens em aberto do documento) contra um
    adiantamento pago já existente (mesma empresa+cliente+valor) -- sem criar documento
    novo nenhum, só uma compensação (Clearing) entre os dois."""
    empresa, documento, ano = chave
    cliente = itens_titulo[0]["Customer"]
    valor = sum((abs(_dec(i["AmountInCompanyCodeCurrency"])) for i in itens_titulo), Decimal("0"))

    # Data da compensação = HOJE (igual ao ajuste_parcelas.py e ao processo manual):
    # precisa ser >= à data de lançamento dos dois itens, e o adiantamento costuma ser
    # mais novo que o título -- usar a data do título faria o SAP recusar.
    hoje = date.today()
    cab = {
        "EMPRESA": empresa, "TIPO_CONTA": "D", "PARCEIRO": cliente, "MOEDA": "BRL",
        "_LANC": hoje, "_DOC": hoje, "TIPO_DOC_COMPENSACAO": TIPO_DOC_COMPENSACAO,
        "TEXTO_CABECALHO": "Concilia adiantamento",
    }
    itens_clearing = [(documento, ano, str(i["AccountingDocumentItem"]).zfill(3)) for i in itens_titulo]
    itens_clearing.append((
        adiantamento["AccountingDocument"], str(adiantamento["FiscalYear"]),
        str(adiantamento["AccountingDocumentItem"]).zfill(3),
    ))

    base = uuid.uuid4().hex[:10].upper()
    env = montar_clearing_itens(cab, itens_clearing, f"ADTO_{base}_C")
    erros = validar(env, WSDL_CLEAR)
    if erros:
        return "ERRO", f"XML de compensação com adiantamento inválido, nada enviado: {erros[0]}"

    adto_doc, adto_ano = adiantamento["AccountingDocument"], adiantamento["FiscalYear"]
    if simular:
        return "SIMULADO_ADIANTAMENTO", (f"conciliaria com adiantamento {adto_doc}/{adto_ano} "
                                         f"({_brl(valor)})")
    try:
        enviar_clearing(s, env)
    except Exception as ex:
        return "ERRO", f"falha ao conciliar com adiantamento {adto_doc}/{adto_ano}: {ex}"

    # compensação é assíncrona -- confirma de verdade antes de dizer que conciliou.
    # Aqui não tem documento novo pra estornar (não criamos nada, só compensamos itens
    # que já existiam) -- se não confirmar, só relata; os dois itens continuam em aberto
    # como estavam, sem nada em dobro.
    if not _aguardar_compensacao(empresa, itens_clearing):
        return "ERRO", (f"compensação com adiantamento {adto_doc}/{adto_ano} não confirmada no tempo de "
                        f"espera (Message Dashboard, ID ADTO_{base}_C). Confira manualmente; os itens "
                        f"continuam em aberto, nada foi duplicado.")

    registrar_conciliacao_adiantamento(empresa, itens_clearing[:-1], itens_titulo, adto_doc, adto_ano)
    return "CONCILIADO_ADIANTAMENTO", (f"compensado com adiantamento {adto_doc}/{adto_ano} "
                                       f"({_brl(valor)}, Message Dashboard ID ADTO_{base}_C)"), {
        "cliente": cliente, "valor": valor, "adiantamento_doc": adto_doc, "adiantamento_ano": adto_ano}


# ----------------------------------------------------------------------
# Reporte: Slack + titulos_ajustados.csv
# ----------------------------------------------------------------------
STATUS_ERRO = ("ERRO", "RECUSADO", "COMPENSACAO_FALHOU")
STATUS_SEM_ACAO = ("OK", "SEM_DUPLICATAS", "AJUSTADO", "SIMULADO",
                   "CONCILIADO_ADIANTAMENTO", "SIMULADO_ADIANTAMENTO",
                   "CONCILIADO_PAGAMENTO", "SIMULADO_PAGAMENTO")


def _brl(v):
    return "R$ " + f"{Decimal(v):,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def _pos_ajuste(r):
    """Resultado da conciliação de parcela feita DEPOIS do reparcelamento (ou None)."""
    return (r.get("info") or {}).get("adiantamento_pos_ajuste")


def contar_conciliados(resultados):
    """Conciliações com adiantamento: título direto + parcela pós-ajuste."""
    return (sum(1 for r in resultados if r["status"] in ("CONCILIADO_ADIANTAMENTO", "CONCILIADO_PAGAMENTO"))
            + sum(1 for r in resultados if (_pos_ajuste(r) or ("",))[0] == "CONCILIADO_ADIANTAMENTO"))


def texto_slack(resultados, total_candidatos):
    cont = defaultdict(int)
    for r in resultados:
        cont[r["status"]] += 1
    ajustados = [r for r in resultados if r["status"] == "AJUSTADO"]
    conciliados = [r for r in resultados if r["status"] == "CONCILIADO_ADIANTAMENTO"]
    pagamentos = [r for r in resultados if r["status"] == "CONCILIADO_PAGAMENTO"]
    conc_pos = [r for r in ajustados if (_pos_ajuste(r) or ("",))[0] == "CONCILIADO_ADIANTAMENTO"]
    erros_pos = [r for r in ajustados if (_pos_ajuste(r) or ("",))[0] == "ERRO"]
    erros = [r for r in resultados if r["status"] in STATUS_ERRO]
    pendentes = {k: v for k, v in cont.items() if k not in STATUS_SEM_ACAO and k not in STATUS_ERRO}

    t = [f"*Ajuste de parcelas – títulos de clientes* ({ambiente()})", "",
         f"*Títulos analisados:* {total_candidatos}",
         f"*Ajustados:* {len(ajustados)}",
         f"*Conciliados com adiantamento:* {len(conciliados) + len(conc_pos)}",
         f"*Pagamentos recebidos conciliados:* {len(pagamentos)}",
         f"*Erros na execução:* {len(erros) + len(erros_pos)}"]
    if pendentes:
        t.append("*Para análise manual:* " + " | ".join(f"{k} {v}" for k, v in sorted(pendentes.items())))

    if ajustados:
        t += ["", "*Títulos ajustados:*"]
        for r in ajustados:
            i = r["info"]
            antigas = ", ".join(f"{p['doc']}/{p['item']}" for p in i["antigas"])
            novas = "; ".join(f"{p['venc']:%d/%m/%Y} {_brl(p['valor'])}" for p in i["novas"])
            t.append(f"• {r['empresa']} {antigas} (NF {r['nf']}, cliente {i['cliente']}, {_brl(i['total'])}) "
                     f"→ *{i['doc_novo']}/{i['ano_novo']}* com {len(i['novas'])} parcela(s): {novas}")
            pos = _pos_ajuste(r)
            if pos and pos[0] == "CONCILIADO_ADIANTAMENTO":
                p = pos[2]
                t.append(f"    ↳ parcela {_brl(p['valor'])} conciliada com adiantamento "
                         f"*{p['adiantamento_doc']}/{p['adiantamento_ano']}*")

    if conciliados:
        t += ["", "*Conciliados com adiantamento pago:*"]
        for r in conciliados:
            i = r["info"]
            t.append(f"• {r['empresa']} {r['documento']}/{r['ano']} (NF {r['nf']}, cliente {i['cliente']}, "
                     f"{_brl(i['valor'])}) → adiantamento *{i['adiantamento_doc']}/{i['adiantamento_ano']}*")

    if pagamentos:
        t += ["", "*Pagamentos recebidos conciliados (busca reversa):*"]
        for r in pagamentos:
            i = r["info"]
            t.append(f"• pagamento *{i['pagamento_doc']}/{i['pagamento_ano']}* ({_brl(i['valor'])}, cliente "
                     f"{i['cliente']}) → {r['empresa']} {r['documento']}/{r['ano']} item {i['item']} (NF {r['nf']})")

    if erros or erros_pos:
        t += ["", "*Erros na execução:*"]
        for r in erros:
            alerta = ":warning: " if r["status"] == "COMPENSACAO_FALHOU" else ""
            t.append(f"• {alerta}{r['empresa']} {r['documento']}/{r['ano']} (NF {r['nf']}) "
                     f"— {r['status']}: {r['detalhe'][:300]}")
        for r in erros_pos:
            t.append(f"• {r['empresa']} {r['documento']}/{r['ano']} (NF {r['nf']}) — ajustado, mas a "
                     f"conciliação da parcela com adiantamento falhou: {_pos_ajuste(r)[1][:300]}")
    return "\n".join(t)


def enviar_slack(texto):
    url, canal, token = (PARAMETROS.get(k, "") for k in ("SLACK_URL", "SLACK_CANAL", "SLACK_TOKEN"))
    if not (url and canal and token):
        print("[aviso] SLACK_URL/SLACK_CANAL/SLACK_TOKEN ausentes no arquivo de parâmetros: relatório não enviado.")
        return False
    partes, atual = [], ""
    for linha in texto.split("\n"):              # Slack aceita ~40 mil caracteres por mensagem
        if len(atual) + len(linha) > 35000:
            partes.append(atual)
            atual = ""
        atual += linha + "\n"
    partes.append(atual)
    ok = True
    for parte in partes:
        try:
            r = requests.post(url, timeout=30, json={"channel": canal, "text": parte}, headers={
                "Authorization": f"Bearer {token}", "Content-Type": "application/json; charset=utf-8"})
            try:
                dados = r.json()
            except ValueError:
                dados = {}
            if not (r.ok and dados.get("ok")):
                ok = False
                print(f"[aviso] Slack recusou: HTTP {r.status_code} {dados.get('error', '')}")
        except Exception as ex:
            ok = False
            print(f"[aviso] falha ao enviar ao Slack: {ex}")
    print("Relatório enviado ao Slack." if ok else "Relatório NÃO enviado ao Slack (veja o aviso acima).")
    return ok


def registrar_conciliacao_adiantamento(empresa, itens_clearing, itens_titulo, adto_doc, adto_ano):
    """titulos_conciliados_adiantamento.csv: uma linha por ITEM de título baixado com
    adiantamento/pagamento (título direto, parcela pós-ajuste e busca reversa -- todos passam
    por conciliar_adiantamento). Lido pelo dashboard: esses itens entram como "Outras formas
    de pagamento" na fila de títulos a serem processados.
    Chave_Titulo = Empresa|Documento|Ano|Item (item com 3 dígitos). Só grava depois da
    compensação confirmada no SAP; falha aqui só avisa, não desfaz nada."""
    arquivo = PARAMETROS.get("ARQUIVO_TITULOS_CONCILIADOS_ADT") or os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "titulos_conciliados_adiantamento.csv")
    valores = {str(i["AccountingDocumentItem"]).zfill(3): abs(_dec(i["AmountInCompanyCodeCurrency"]))
               for i in itens_titulo}
    try:
        novo = not os.path.exists(arquivo)
        with open(arquivo, "a", encoding="utf-8", newline="") as f:
            if novo:
                f.write("data,Chave_Titulo,adiantamento,valor\r\n")
            for documento, ano, item in itens_clearing:
                f.write(f"{date.today().isoformat()},{empresa}|{documento}|{ano}|{item},"
                        f"{adto_doc}/{adto_ano},{valores.get(item, '')}\r\n")
    except Exception as ex:
        print(f"[aviso] não consegui registrar a conciliação em {arquivo}: {ex}")


def registrar_titulos_ajustados(ajustados, conciliados):
    """titulos_ajustados.csv (data,ajustados,conciliados): soma na linha de hoje ou cria a linha.
    ajustados   = títulos reparcelados pelo XML;
    conciliados = compensações com adiantamento/pagamento (título, parcela pós-ajuste e busca reversa).
    Arquivo no formato antigo (data,quantidade) é convertido: a quantidade vira 'ajustados'."""
    arquivo = PARAMETROS.get("ARQUIVO_TITULOS_AJUSTADOS") or os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "titulos_ajustados.csv")
    hoje = date.today().isoformat()
    dados = {}
    try:
        if os.path.exists(arquivo):
            with open(arquivo, encoding="utf-8-sig", newline="") as f:
                for row in csv.DictReader(f):
                    d = (row.get("data") or "").strip()
                    if not d:
                        continue
                    aj = row.get("ajustados")
                    if aj in (None, ""):
                        aj = row.get("quantidade")          # formato antigo
                    dados[d] = [int(float(aj or 0)), int(float(row.get("conciliados") or 0))]
        atual = dados.get(hoje, [0, 0])
        dados[hoje] = [atual[0] + ajustados, atual[1] + conciliados]
        with open(arquivo, "w", encoding="utf-8", newline="") as f:
            f.write("data,ajustados,conciliados\r\n")
            for d in sorted(dados):
                f.write(f"{d},{dados[d][0]},{dados[d][1]}\r\n")
        print(f"titulos_ajustados.csv: {hoje} = {dados[hoje][0]} ajustado(s), "
              f"{dados[hoje][1]} conciliado(s)  ({arquivo})")
    except Exception as ex:
        print(f"[aviso] não consegui atualizar {arquivo}: {ex}")


# Documentos que a passada 1 alterou (ou simularia alterar) -- ficam fora da busca reversa,
# porque as partidas lidas no início da execução não refletem mais o SAP.
STATUS_DOC_ALTERADO = ("AJUSTADO", "SIMULADO", "CONCILIADO_ADIANTAMENTO", "SIMULADO_ADIANTAMENTO",
                       "COMPENSACAO_FALHOU", "ERRO")


def _chave_doc(l):
    return (l["CompanyCode"], l["AccountingDocument"], str(l["FiscalYear"]))


def conciliar_pagamentos_recebidos(linhas, idx_adiantamentos, docs_alterados, sessao_je, simular,
                                   limite=None):
    """Passada 2 (busca ao contrário): para cada pagamento recebido em aberto (documento 140...)
    que NÃO foi usado na passada 1, procura nas partidas em aberto uma com a mesma chave
    empresa + cliente + valor (tolerância TOLERANCIA) e concilia as duas.

    * Não encontrar nada é normal e silencioso -- só os casos encontrados viram resultado.
    * Com mais de uma partida possível, usa a de vencimento mais antigo (depois a de lançamento
      mais antigo) e registra quantas havia.
    * Cada pagamento e cada partida são usados uma vez só."""
    abertas = defaultdict(list)
    for l in linhas:
        if _chave_doc(l) in docs_alterados or not str(l.get("Customer") or "").strip():
            continue
        if _dec(l.get("AmountInCompanyCodeCurrency") or 0) <= 0:
            continue
        abertas[(l["CompanyCode"], l["Customer"])].append(l)
    for lst in abertas.values():
        lst.sort(key=lambda l: (_data(l.get("NetDueDate")) or date.max, _data(l.get("Dt_lcto")) or date.max,
                                l["AccountingDocument"], str(l["AccountingDocumentItem"]).zfill(3)))

    pagamentos = [(chave, p) for chave, lst in idx_adiantamentos.items() for p in lst
                  if str(p.get("AccountingDocument") or "").startswith(PREFIXO_PAGAMENTO)]
    pagamentos.sort(key=lambda cp: (_data(cp[1].get("PostingDate")) or date.min, cp[1]["AccountingDocument"]))

    resultados = []
    for chave_idx, p in pagamentos:
        if limite and len(resultados) >= limite:
            break
        lista = abertas.get(chave_idx) or []
        possiveis = [l for l in lista if abs(abs(_dec(l["AmountInCompanyCodeCurrency"])) - p["_valor"]) <= TOLERANCIA]
        if not possiveis:
            continue
        escolhida = possiveis[0]
        lista.remove(escolhida)
        idx_adiantamentos[chave_idx].remove(p)          # não reaproveita o pagamento

        empresa, documento, ano = _chave_doc(escolhida)
        nf = numero_nf(escolhida.get("DocumentReferenceID_2", ""))
        print(f"\n[pagto {p['AccountingDocument']}/{p['FiscalYear']}] {empresa} {documento}/{ano} "
              f"item {str(escolhida['AccountingDocumentItem']).zfill(3)} | NF {nf} | cliente {escolhida['Customer']}")
        try:
            res = conciliar_adiantamento(sessao_je, (empresa, documento, ano), [escolhida], p, simular)
        except Exception as ex:
            res = ("ERRO", str(ex)[:400])

        status = {"CONCILIADO_ADIANTAMENTO": "CONCILIADO_PAGAMENTO",
                  "SIMULADO_ADIANTAMENTO": "SIMULADO_PAGAMENTO"}.get(res[0], res[0])
        detalhe = res[1].replace("com adiantamento", "com pagamento")
        if len(possiveis) > 1:
            detalhe += (f" | {len(possiveis)} partidas possíveis com esse valor: usada a de vencimento "
                        f"mais antigo ({_data(escolhida.get('NetDueDate')):%d/%m/%Y})")
        print(f"   -> {status}: {detalhe}")
        info = dict(res[2]) if len(res) > 2 else {}
        info.update({"pagamento_doc": p["AccountingDocument"], "pagamento_ano": str(p["FiscalYear"]),
                     "valor": abs(_dec(escolhida["AmountInCompanyCodeCurrency"])),
                     "cliente": escolhida["Customer"],
                     "item": str(escolhida["AccountingDocumentItem"]).zfill(3)})
        resultados.append({"status": status, "detalhe": detalhe, "empresa": empresa, "documento": documento,
                           "ano": ano, "nf": nf, "info": info})
    return resultados, len(pagamentos)


def main():
    ap = argparse.ArgumentParser(description="Ajusta parcelas de títulos de cliente pelo XML da NF.")
    ap.add_argument("--simular", action="store_true", help="não lança nada, só mostra")
    ap.add_argument("--empresa", help="filtra uma empresa (ex.: BR01)")
    ap.add_argument("--doc", help="um documento contábil específico")
    ap.add_argument("--limite", type=int, help="processa só os N primeiros títulos")
    ap.add_argument("--parametros", help="caminho do parametros_prod.txt (padrão: ao lado do script)")
    ap.add_argument("--sem-pagamentos", action="store_true",
                    help="não faz a busca reversa (pagamentos recebidos 140... x partidas em aberto)")
    a = ap.parse_args()

    print("=" * 78)
    print(f"AMBIENTE SAP: {ambiente()}  ({BASE_URL})  usuário {JE_USER}"
          + ("   [SIMULAÇÃO]" if a.simular else ""))
    if urlsplit(URL_CDS).netloc != _soap.netloc:
        sys.exit(f"SAP_1_URL ({urlsplit(URL_CDS).netloc}) e SAP_SOAP_URL ({_soap.netloc}) são de "
                 f"ambientes diferentes. Ajuste o parametros_prod.txt.")
    if not os.path.exists(WSDL_POST):
        print("[aviso] WSDLs não encontrados em apis_sap\\ -> envio sem validação prévia")
    linhas = ler_partidas_sap(a.empresa, a.doc)
    candidatos = titulos_candidatos(linhas)
    print(f"{len(linhas)} partida(s) em aberto | {len(candidatos)} título(s) vencendo na data de lançamento")

    linhas_adto = ler_adiantamentos(a.empresa)
    idx_adiantamentos = indice_adiantamentos(linhas_adto)
    print(f"{len(linhas_adto)} adiantamento(s) pago(s) em aberto (candidatos à conciliação automática)")
    print("=" * 78)
    if a.limite:
        candidatos = candidatos[:a.limite]

    sessao_je = requests.Session()
    sessao_je.auth = (JE_USER or "", JE_PASS or "")
    resultados = []

    for n, (chave, itens) in enumerate(candidatos, start=1):
        empresa, documento, ano = chave
        ref = itens[0].get("DocumentReferenceID_2", "")
        nf = numero_nf(ref)
        print(f"\n[{n}/{len(candidatos)}] {empresa} {documento}/{ano} | NF {nf} | cliente {itens[0]['Customer']}")
        try:
            res = analisar_e_ajustar(chave, itens, nf, sessao_je, a.simular, idx_adiantamentos)
        except Exception as ex:
            res = ("ERRO", str(ex)[:400])
        status, detalhe = res[0], res[1]
        print(f"   -> {status}: {detalhe}")
        resultados.append({"status": status, "detalhe": detalhe, "empresa": empresa, "documento": documento,
                           "ano": ano, "nf": nf, "info": res[2] if len(res) > 2 else {}})

    if not a.sem_pagamentos:
        print("\n" + "=" * 78 + "\nPAGAMENTOS RECEBIDOS (" + PREFIXO_PAGAMENTO + "...) x PARTIDAS EM ABERTO")
        docs_alterados = {(r["empresa"], r["documento"], r["ano"]) for r in resultados
                          if r["status"] in STATUS_DOC_ALTERADO}
        res_pag, n_pag = conciliar_pagamentos_recebidos(linhas, idx_adiantamentos, docs_alterados,
                                                        sessao_je, a.simular, a.limite)
        print(f"\n{n_pag} pagamento(s) analisado(s) | {len(res_pag)} com partida correspondente")
        resultados += res_pag

    print("\n" + "=" * 78 + "\nRESUMO")
    cont = defaultdict(int)
    for r in resultados:
        cont[r["status"]] += 1
    print("   " + " | ".join(f"{k} {v}" for k, v in sorted(cont.items())) if cont else "   nada a fazer")
    for r in resultados:
        if r["status"] not in STATUS_SEM_ACAO:
            print(f"   [{r['status']}] {r['empresa']} {r['documento']}/{r['ano']} NF {r['nf']}: {r['detalhe']}")

    if a.simular:
        print("\n[SIMULAÇÃO] Slack e titulos_ajustados.csv não são atualizados.")
        return
    print()
    registrar_titulos_ajustados(cont.get("AJUSTADO", 0), contar_conciliados(resultados))
    houve_conciliacao = contar_conciliados(resultados) > 0
    houve_erro_pos = any((_pos_ajuste(r) or ("",))[0] == "ERRO" for r in resultados)
    if (cont.get("AJUSTADO") or houve_conciliacao or houve_erro_pos
            or any(r["status"] in STATUS_ERRO for r in resultados)):
        enviar_slack(texto_slack(resultados, len(candidatos)))
    else:
        print("Nada ajustado e nenhum erro: Slack não enviado.")


def _analisar_e_ajustar_nf(chave, itens, nf, sessao_je, simular):
    cards = buscar_cards(nf)
    if not cards:
        return "SEM_CARD", f"NF {nf} não encontrada no pipe {PIPE_ID}"
    validos, problemas = xml_do_titulo(nf, itens[0]["Customer"], cards)
    if not validos:
        return "XML_DIVERGENTE", " | ".join(problemas)
    if len({c["id"] for c, _ in validos}) > 1:
        return "CARD_AMBIGUO", "mais de um card com XML desta NF/cliente: " + ", ".join(c["id"] for c, _ in validos)

    _, nfe = validos[0]
    if nfe["cStat"] and nfe["cStat"] != "100":
        return "NF_NAO_AUTORIZADA", f"cStat {nfe['cStat']} no XML"
    status, detalhe = comparar(itens, nfe)
    if status != "AJUSTAR":
        return status, detalhe
    return reparcelar(sessao_je, montar_entrada(chave, itens, nfe, nf), simular)


def _localizar_item_novo(empresa, doc_novo, ano_novo, valor, venc, posicao_reserva, simular):
    """Acha no documento novo (já lançado) o item em aberto com esse valor/vencimento --
    o SAP pode numerar os itens diferente da ordem enviada, então não dá pra confiar só
    na posição (mesma abordagem do ajuste_parcelas.py, que localiza pelo valor). Na
    simulação o documento não existe, então usa a posição esperada."""
    if simular:
        return str(posicao_reserva).zfill(3)
    for l in ler_linhas_documento(empresa, doc_novo, ano_novo):
        if str(l.get("ClearingJournalEntry") or "").strip():
            continue
        if _dec(l.get("AmountInCompanyCodeCurrency") or 0) <= 0:   # parcela do cliente = débito
            continue
        if abs(_dec(l["AmountInCompanyCodeCurrency"]) - valor) > TOLERANCIA:
            continue
        if _data(l.get("NetDueDate")) not in (venc, None):
            continue
        return str(l["AccountingDocumentItem"]).zfill(3)
    return str(posicao_reserva).zfill(3)


def _checar_adiantamento_pos_ajuste(resultado, chave, data_lcto, sessao_je, simular, idx_adiantamentos):
    """Mesmo com o título AJUSTADO (reparcelado pelo XML), é comum a 1ª parcela nova
    vencer no mesmo dia do lançamento (1ª duplicata do XML com 0 dias de prazo) --
    isso também costuma ser adiantamento, só que agora no documento NOVO, não mais
    no original. Confere cada parcela nova contra o lançamento; na primeira que bater
    e tiver adiantamento correspondente, concilia só essa parcela (item isolado do
    documento novo) e anota no detalhe/info, sem mudar o status geral (AJUSTADO/SIMULADO)."""
    status, detalhe, info = resultado
    empresa = chave[0]
    cliente = info.get("cliente")
    doc_novo, ano_novo = info.get("doc_novo"), info.get("ano_novo")
    novas = info.get("novas") or []

    for posicao, p in enumerate(novas, start=2):  # item 002, 003... (001 é a contrapartida)
        if p["venc"] != data_lcto:
            continue
        adiantamento = buscar_adiantamento(idx_adiantamentos, empresa, cliente, p["valor"])
        if adiantamento is None:
            continue
        item = _localizar_item_novo(empresa, doc_novo, ano_novo, p["valor"], p["venc"], posicao, simular)
        item_novo = [{"AccountingDocumentItem": item,
                      "AmountInCompanyCodeCurrency": str(p["valor"]), "Customer": cliente,
                      "Dt_lcto": data_lcto.isoformat()}]
        res_conc = conciliar_adiantamento(sessao_je, (empresa, doc_novo, ano_novo), item_novo, adiantamento, simular)
        if res_conc[0] == "ERRO":
            extra = f"parcela de {data_lcto:%d/%m/%Y} ({_brl(p['valor'])}) parecia adiantamento mas falhou: {res_conc[1]}"
        else:
            extra = f"parcela de {data_lcto:%d/%m/%Y} ({_brl(p['valor'])}) também era adiantamento -> {res_conc[1]}"
        info = dict(info)
        info["adiantamento_pos_ajuste"] = res_conc
        return status, detalhe + " | " + extra, info
    return resultado


def analisar_e_ajustar(chave, itens, nf, sessao_je, simular, idx_adiantamentos):
    """Tenta primeiro o ajuste normal por NF/XML.
    - Se ajustou (ou simularia ajustar) e alguma parcela nova também vence no dia do
      lançamento, tenta conciliar essa parcela com um adiantamento -- é comum a 1ª
      parcela do reparcelamento gerar essa mesma demanda.
    - Se não ajustou (SEM_CARD, XML_DIVERGENTE, etc.), tenta conciliar com um
      adiantamento (mesma empresa+cliente+valor): primeiro a(s) partida(s) que vencem
      na data de lançamento, uma a uma; se nenhuma bater, o título inteiro."""
    resultado = _analisar_e_ajustar_nf(chave, itens, nf, sessao_je, simular)
    status = resultado[0]

    if status in ("AJUSTADO", "SIMULADO"):
        if len(resultado) > 2 and resultado[2].get("novas"):
            data_lcto = _data(itens[0].get("Dt_lcto"))
            resultado = _checar_adiantamento_pos_ajuste(
                resultado, chave, data_lcto, sessao_je, simular, idx_adiantamentos)
        return resultado

    empresa = chave[0]
    cliente = itens[0]["Customer"]

    # 1) partida(s) com vencimento == lançamento, individualmente
    for i in itens:
        venc, lanc = _data(i.get("NetDueDate")), _data(i.get("Dt_lcto"))
        if venc is None or venc != lanc:
            continue
        adiantamento = buscar_adiantamento(idx_adiantamentos, empresa, cliente,
                                           abs(_dec(i["AmountInCompanyCodeCurrency"])))
        if adiantamento is not None:
            return conciliar_adiantamento(sessao_je, chave, [i], adiantamento, simular)

    # 2) título inteiro (adiantamento cobrindo a NF toda)
    if len(itens) > 1:
        valor_titulo = sum((abs(_dec(i["AmountInCompanyCodeCurrency"])) for i in itens), Decimal("0"))
        adiantamento = buscar_adiantamento(idx_adiantamentos, empresa, cliente, valor_titulo)
        if adiantamento is not None:
            return conciliar_adiantamento(sessao_je, chave, itens, adiantamento, simular)

    return resultado  # sem adiantamento correspondente -- mantém o status original p/ análise

if __name__ == "__main__":
    main()