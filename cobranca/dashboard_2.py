import os
import json
import requests
import pandas as pd
import io

from datetime import datetime, timedelta
import csv
from flask import Flask, render_template_string, request, send_file, jsonify


app = Flask(__name__)


# ============================================================
# 1. REGRAS E FERIADOS
# ============================================================

FERIADOS = {
    "2026-01-01",
    "2026-04-03",
    "2026-04-21",
    "2026-05-01",
    "2026-06-04",
    "2026-09-07",
    "2026-10-12",
    "2026-11-02",
    "2026-11-15",
    "2026-11-20",
    "2026-12-25",
}


def is_util_day(date_str):
    try:
        d = datetime.strptime(date_str, "%Y-%m-%d")
        return d.weekday() < 5 and date_str not in FERIADOS
    except (ValueError, TypeError):
        return False


def proximo_dia_util(date_str):
    try:
        d = datetime.strptime(date_str, "%Y-%m-%d") + timedelta(days=1)
        while True:
            s = d.strftime("%Y-%m-%d")
            if d.weekday() < 5 and s not in FERIADOS:
                return s
            d += timedelta(days=1)
    except (ValueError, TypeError):
        return date_str


# ============================================================
# 2. PARÂMETROS
# ============================================================

def carregar_parametros(arquivo):
    parametros = {}
    if not os.path.exists(arquivo):
        print(f"Aviso: Arquivo {arquivo} não encontrado.")
        return parametros
    with open(arquivo, "r", encoding="utf-8") as f:
        for linha in f:
            linha = linha.strip()
            if not linha or linha.startswith("#"):
                continue
            if "=" in linha:
                chave, valor = linha.split("=", 1)
                parametros[chave.strip()] = (
                    valor.strip().strip('"').strip("'")
                )
    return parametros


# ============================================================
# 3. TRATAMENTO DE DATAS SAP
# ============================================================

def formatar_data_sap_iso(valor):
    """Converte datas do SAP para YYYY-MM-DD (ou None). Aceita /Date(ts)/,
    epoch em ms (int/float), YYYY-MM-DD, DD/MM/YYYY, DD.MM.YYYY.
    Blindado para NUNCA devolver algo que não seja str ou None — um NaN
    (float) escapando daqui é "verdadeiro" num if e quebra tudo mais na frente."""
    try:
        # pd.isna cobre None, NaN, NaT; isinstance cobre listas/arrays que
        # fariam pd.isna explodir com "truth value ambiguous"
        if valor is None:
            return None
        if isinstance(valor, float) and (valor != valor):  # NaN != NaN
            return None
        if isinstance(valor, (pd.Timestamp,)) or hasattr(valor, "strftime"):
            return valor.strftime("%Y-%m-%d")
        if isinstance(valor, (int, float)):
            # epoch em milissegundos vindo cru (sem o wrapper /Date()/)
            if valor == 0:
                return None
            return pd.to_datetime(valor, unit="ms").strftime("%Y-%m-%d")
        if pd.isna(valor):
            return None
    except (TypeError, ValueError):
        pass  # segue tentando como string

    if not valor:
        return None
    v = str(valor).strip()
    if not v or v.lower() == "nan":
        return None

    try:
        if "/Date(" in v:
            timestamp = int(v.replace("/Date(", "").replace(")/", "").split("+")[0].split("-")[0])
            return pd.to_datetime(timestamp, unit="ms").strftime("%Y-%m-%d")
        if len(v) == 10 and v[4] == "-" and v[7] == "-":
            return datetime.strptime(v, "%Y-%m-%d").strftime("%Y-%m-%d")
        if len(v) == 10 and v[2] == "/" and v[5] == "/":
            return datetime.strptime(v, "%d/%m/%Y").strftime("%Y-%m-%d")
        if len(v) == 10 and v[2] == "." and v[5] == ".":
            return datetime.strptime(v, "%d.%m.%Y").strftime("%Y-%m-%d")
        return pd.to_datetime(v, dayfirst=True).strftime("%Y-%m-%d")
    except Exception:
        return None


def parsear_creation_time(valor):
    """
    SAP retorna CreationTime como ISO 8601 duration: PT17H34M09S → 17:34:09
    Aceita também HH:MM:SS convencional.
    """
    import re as _re
    if not valor or (isinstance(valor, float) and pd.isna(valor)):
        return ""
    v = str(valor).strip()
    if not v:
        return ""
    if v.upper().startswith("PT"):
        try:
            h = int(_re.search(r"(\d+)H", v, _re.I).group(1)) if _re.search(r"\dH", v, _re.I) else 0
            m = int(_re.search(r"(\d+)M", v, _re.I).group(1)) if _re.search(r"\dM", v, _re.I) else 0
            s = int(_re.search(r"(\d+)S", v, _re.I).group(1)) if _re.search(r"\dS", v, _re.I) else 0
            return f"{h:02d}:{m:02d}:{s:02d}"
        except Exception:
            return ""
    if len(v) == 8 and v[2] == ":" and v[5] == ":":
        return v
    return ""




# ============================================================
# 3B. TÍTULOS NOS BANCOS — regras, FIDC e motor de elegibilidade/sugestão
#     (usado pela primeira aba do dashboard)
# ============================================================

BANKS = ["Safra", "ABC", "Sofisa", "Banco do Brasil", "Santander"]

# elegibilidade e sugestão de banco só existem pra esses 3 — BB e Santander
# continuam aparecendo na carteira/aging, mas nunca entram na tabela de decisão
ROUTABLE_BANKS = ["Safra", "ABC", "Sofisa"]

REGRAS_CSV_PATH = os.environ.get("REGRAS_CSV_PATH", "/var/www/api_cobranca/regras_bancos.csv")
REGRAS_CSV_CAMPOS = ["banco", "limite_carteira", "concentracao", "valor_max_titulo", "prazo_max"]

BANK_RULES_PADRAO = {
    "Safra":  {"limite_carteira": 1_000_000, "concentracao": 0.10, "valor_max_titulo": None, "prazo_max": 180},
    "ABC":    {"limite_carteira": 1_000_000, "concentracao": 0.10, "valor_max_titulo": None, "prazo_max": 180},
    "Sofisa": {"limite_carteira": 1_000_000, "concentracao": 0.10, "valor_max_titulo": None, "prazo_max": 180},
    "Banco do Brasil": {"limite_carteira": 1_000_000, "concentracao": 0.15, "valor_max_titulo": None, "prazo_max": None},
    "Santander": {"limite_carteira": 1_000_000, "concentracao": 0.15, "valor_max_titulo": None, "prazo_max": None},
}


def _num_ou_none(v):
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def salvar_regras_bancos(regras, caminho=None):
    caminho = caminho or REGRAS_CSV_PATH
    with open(caminho, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=REGRAS_CSV_CAMPOS)
        writer.writeheader()
        for banco in BANKS:
            r = regras.get(banco, {})
            writer.writerow({
                "banco": banco,
                "limite_carteira": r.get("limite_carteira") if r.get("limite_carteira") is not None else "",
                "concentracao": r.get("concentracao") if r.get("concentracao") is not None else "",
                "valor_max_titulo": r.get("valor_max_titulo") if r.get("valor_max_titulo") is not None else "",
                "prazo_max": r.get("prazo_max") if r.get("prazo_max") is not None else "",
            })


def carregar_regras_bancos(caminho=None):
    caminho = caminho or REGRAS_CSV_PATH
    regras = {b: dict(BANK_RULES_PADRAO[b]) for b in BANKS}

    if not os.path.exists(caminho):
        # primeira vez rodando: já cria o csv com os valores padrão, pra
        # existir um arquivo de apoio editável desde o início
        try:
            salvar_regras_bancos(regras, caminho)
        except Exception as e:
            print(f"Aviso: não consegui criar {caminho} com os valores padrão: {e}")
        return regras

    try:
        with open(caminho, "r", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                banco = (row.get("banco") or "").strip()
                if banco not in regras:
                    continue
                prazo = _num_ou_none(row.get("prazo_max"))
                regras[banco] = {
                    "limite_carteira": _num_ou_none(row.get("limite_carteira")),
                    "concentracao": _num_ou_none(row.get("concentracao")),
                    "valor_max_titulo": _num_ou_none(row.get("valor_max_titulo")),
                    "prazo_max": int(prazo) if prazo is not None else None,
                }
    except Exception as e:
        print(f"Erro ao ler {caminho}, usando regras padrão: {e}")
        return {b: dict(BANK_RULES_PADRAO[b]) for b in BANKS}

    return regras


BANK_RULES = carregar_regras_bancos()

# faixas de vencimento (dias até o vencimento a partir de hoje) para o gráfico
# de aging da carteira — (rótulo, dias mínimo, dias máximo); None = sem limite.
# O que já venceu (dias <= 0) é dividido em "Atrasado" (até 9 dias) e
# "Protestado" (mais de 9 dias vencido).
AGING_BUCKETS = [
    ("Protestado", None, -10),
    ("Atrasado", -9, 0),
    ("1-30", 1, 30),
    ("31-60", 31, 60),
    ("61-90", 61, 90),
    ("91-120", 91, 120),
    ("121-150", 121, 150),
    ("151-180", 151, 180),
    ("180+", 181, None),
]


def bucket_por_dias(dias):
    for label, minimo, maximo in AGING_BUCKETS:
        if minimo is not None and dias < minimo:
            continue
        if maximo is not None and dias > maximo:
            continue
        return label
    return None


# de/para fixo: 3 primeiras letras do código HouseBank (Banco_da_empresa.xlsx) -> banco
# ABC01 -> ABC | SFR01/02/03 -> Safra | SOF.. -> Sofisa (ainda não visto na base, mas
# mantido aqui para quando o Sofisa passar a usar HouseBank também) | BB0.. -> Banco
# do Brasil | SAN.. -> Santander
HOUSEBANK_PREFIX = {
    "ABC": "ABC", "SFR": "Safra", "SOF": "Sofisa",
    "BB0": "Banco do Brasil", "SAN": "Santander",
}

# palavras-chave no campo AssignmentReference que indicam título já
# direcionado a um desses bancos, mesmo sem HouseBank preenchido.
# ABC: só "ABC-CV" conta como carteira de verdade do banco — "ABC" sozinho
# (sem o -CV) é uma composição diferente ("carteira simples"), que NÃO entra
# no limite do banco, só fica registrada à parte pra transparência.
ASSIGNMENT_KEYWORDS = [("ABC-CV", "ABC"), ("SAFRA", "Safra"), ("SOFISA", "Sofisa")]


def is_carteira_simples_abc(ref):
    """Referência menciona ABC mas NÃO é 'ABC-CV' — é a composição de
    'carteira simples', que não soma no limite do banco ABC, só vira uma
    observação de valor à parte no card (para transparência)."""
    if not ref:
        return False
    up = str(ref).strip().upper()
    return "ABC" in up and "ABC-CV" not in up

# FIDCs: match EXATO do AssignmentReference (não é "contém", igual aos bancos —
# o valor precisa ser idêntico a uma dessas strings). "FIDC-GAMA" e
# "FIDC - GAMA" são a mesma coisa escrita de dois jeitos na origem (com/sem
# espaço ao redor do traço) — as duas grafias contam para o MESMO total.
FIDC_ALIASES = {  # nomes de exemplo: na empresa ficam os FIDCs parceiros
    "FIDC-ALFA": "ALFA",
    "FIDC-BETA": "BETA",
    "FIDC-GAMA": "GAMA",
    "FIDC - GAMA": "GAMA",
    "FIDC-DELTA": "DELTA",
    "FIDC-EPSILON": "EPSILON",
}
FIDC_LABELS = ["ALFA", "BETA", "GAMA", "DELTA", "EPSILON"]  # rótulos únicos exibidos

# títulos com essas referências não devem aparecer na lista de decisão/sugestão
# (match exato, sem diferenciar maiúsc./minúsc. e ignorando espaços nas pontas)
EXCLUDED_ASSIGNMENT_REFS = {
    "ABT. EM AGENCIMENT",
    "Abt. em agenciamen",
    "CARTÃO BNB",
    "Clt. irá recusar",
    "Pgto. via cartão",
    "ABT. EM DEVOLUÇÃO",
    "CRÉD. EMP. FECHO",
    "CRÉD. P/ VEND. FUT",
    "ENCONTRO DE NF",
    "EXPORTAÇÃO",
    "Exportação",
    "NF RECUSADA",
    "TED",
    "VAI PAGAR NO PIX",
}


# versão normalizada (maiúsculas, sem espaços nas pontas) — assim as entradas
# de EXCLUDED_ASSIGNMENT_REFS podem estar escritas em caixa mista e ainda
# assim batem com o valor vindo do SAP, independente de maiúsc./minúsc.
_EXCLUDED_ASSIGNMENT_REFS_NORM = {r.strip().upper() for r in EXCLUDED_ASSIGNMENT_REFS}


def is_excluded_reference(ref):
    if not ref:
        return False
    return str(ref).strip().upper() in _EXCLUDED_ASSIGNMENT_REFS_NORM


# formas de pagamento aceitas: vazio ('') ou 'E'. Qualquer outra forma de
# pagamento é eliminada, nos mesmos lugares onde EXCLUDED_ASSIGNMENT_REFS roda.
ALLOWED_PAYMENT_METHODS = {"", "E"}


def is_excluded_payment_method(pm):
    """True quando PaymentMethod é diferente de '' e 'E' (linha deve sumir).
    Nulo/NaN/espaços contam como vazio ('') e são mantidos."""
    if pm is None or (isinstance(pm, float) and pd.isna(pm)):
        return False
    return str(pm).strip().upper() not in ALLOWED_PAYMENT_METHODS


# ============================================================


def bank_from_housebank(code):
    if not code:
        return None
    prefixo = str(code).strip().upper()[:3]
    return HOUSEBANK_PREFIX.get(prefixo)


def bank_from_assignment(ref):
    if not ref:
        return None
    up = str(ref).upper()
    for kw, bank in ASSIGNMENT_KEYWORDS:
        if kw in up:
            return bank
    return None


def fidc_from_assignment(ref):
    """Match EXATO (não substring) contra os aliases de FIDC, devolvendo o
    rótulo canônico (as duas grafias do mesmo FIDC caem no mesmo rótulo). Tem
    prioridade sobre qualquer resolução de banco: se o título está marcado
    pra um FIDC, ele não conta como carteira de nenhum banco, mesmo que
    tenha HouseBank preenchido."""
    if not ref:
        return None
    val = str(ref).strip()
    return FIDC_ALIASES.get(val)


# ============================================================
# 4. BUSCA NO SAP (OData — mesma entidade das suas duas consultas
#    de Power Query: YY1_API_ITENS_CLIENTE_CDS)
# ============================================================

def _sap_get(url, usuario, senha, filtro, select, top=5000):
    """Busca TODAS as páginas do resultado, seguindo o link de continuação
    do OData — d.__next no v2, @odata.nextLink no v4.

    Reativado porque encontramos evidência concreta: a consulta de carteira
    (sem filtro de HouseBank, portanto com MUITO mais linhas que as outras
    duas consultas) estava com valor por baixo do real pra um banco
    específico, mesmo com quantidade de títulos batendo — sintoma clássico
    de corte de página no meio do resultado."""
    params = {"$filter": filtro, "$select": select, "$format": "json", "$top": top}
    headers = {"Accept": "application/json"}

    registros = []
    next_url = url
    next_params = params
    paginas = 0

    while next_url:
        resp = requests.get(next_url, params=next_params, headers=headers, auth=(usuario, senha), timeout=60)
        resp.raise_for_status()
        dados = resp.json()
        paginas += 1

        if "d" in dados and "results" in dados["d"]:
            lote = dados["d"]["results"]
            next_link = dados["d"].get("__next")
        elif "value" in dados:
            lote = dados["value"]
            next_link = dados.get("@odata.nextLink")
        else:
            lote, next_link = [], None

        registros.extend(lote)

        # o link de continuação já vem com todos os parâmetros embutidos —
        # não reenviar $filter/$select/$top junto, senão duplica/conflita
        next_url = next_link
        next_params = None

    print(f"  SAP: {len(registros)} registro(s) em {paginas} página(s) — filtro: {filtro}")
    return registros


def obter_dados_recebiveis():
    parametros = carregar_parametros("/var/www/api_cobranca/parametros_prod.txt")
    sap_url = parametros.get("SAP_1_URL", "")
    sap_usuario = parametros.get("SAP_1_USUARIO", "")
    sap_senha = parametros.get("SAP_1_SENHA", "")

    saldo_normalizado = []      # [{customer, bank, valor}]
    itens_normalizados = []     # [{doc, item, postingDate, dueDate, amount, customer, assignmentRef, preAssignedBank}]
    venc7_por_banco = {b: 0.0 for b in BANKS}   # valor vencendo nos próximos 7 dias
    venc15_por_banco = {b: 0.0 for b in BANKS}  # valor vencendo nos próximos 15 dias
    venc30_por_banco = {b: 0.0 for b in BANKS}  # valor da carteira de cada banco que vence nos próximos 30 dias
    qtd_titulos_por_banco = {b: 0 for b in BANKS}  # qtd de títulos (linhas AccountingDocument+FiscalYear+AccountingDocumentItem) por banco
    valor_por_fidc = {f: 0.0 for f in FIDC_LABELS}     # carteira por FIDC (match exato do AssignmentReference)
    qtd_titulos_por_fidc = {f: 0 for f in FIDC_LABELS}
    valor_carteira_simples_abc = 0.0  # "ABC" sem -CV — observação à parte, não soma na carteira do ABC
    qtd_carteira_simples_abc = 0
    aging_labels = [label for label, _, _ in AGING_BUCKETS]
    aging_por_banco = {label: {b: 0.0 for b in BANKS} for label in aging_labels}  # valor em aberto por faixa de vencimento x banco
    vencido_por_banco = {b: 0.0 for b in BANKS}  # valor já vencido (Atrasado+Protestado) por banco, pra abater da carteira
    erro_saldo = None
    erro_itens = None
    erro_aging = None
    hoje = datetime.now().date()

    if sap_url:
        # ---- consulta 1: CARTEIRA POR BANCO — exatamente a consulta validada:
        # todo título em aberto (Customer <> '' e ClearingDate = null), sem
        # filtrar por HouseBank. O banco de cada linha é resolvido depois em
        # Python: pelo HouseBank quando preenchido, senão pelo AssignmentReference
        # (título já direcionado mesmo sem HouseBank atribuído ainda) ----
        try:
            registros = _sap_get(
                sap_url, sap_usuario, sap_senha,
                filtro="Customer ne '' and Customer ne '20012001' and Customer ne '40014001' and Customer ne '70017001' and Customer ne '80018001' and ClearingDate eq null and AccountingDocumentType eq 'RV' and TransactionCurrency eq 'BRL'",
                select="AccountingDocument,FiscalYear,AccountingDocumentItem,AmountInCompanyCodeCurrency,Customer,CustomerName,AssignmentReference,HouseBank,PaymentMethod,PaymentTerms",
            )
            if registros:
                df = pd.DataFrame(registros)
                for col in ["AssignmentReference", "HouseBank", "PaymentMethod"]:
                    if col not in df.columns:
                        df[col] = None
                df["AmountInCompanyCodeCurrency"] = pd.to_numeric(
                    df["AmountInCompanyCodeCurrency"], errors="coerce"
                ).fillna(0.0)
                df = df[df["Customer"].astype(bool)]
                # forma de pagamento diferente de '' e 'E' — elimina a linha
                df = df[~df["PaymentMethod"].apply(is_excluded_payment_method)]

                def resolver_banco(row):
                    # referência de movimentação que não é título de banco/FIDC
                    # (TED, encontro de NF, exportação, etc.) — não conta em
                    # carteira nenhuma
                    if is_excluded_reference(row["AssignmentReference"]):
                        return None
                    # FIDC manda mais que tudo: se o AssignmentReference bate exato
                    # com um dos FIDCs, o título NÃO conta como carteira de banco
                    # nenhum, mesmo que tenha HouseBank preenchido.
                    if fidc_from_assignment(row["AssignmentReference"]):
                        return None
                    # "ABC" sozinho (sem -CV) é carteira simples — não conta na
                    # carteira do banco (nem via HouseBank), só vira observação
                    # à parte, calculada mais abaixo.
                    if is_carteira_simples_abc(row["AssignmentReference"]):
                        return None
                    # Depois disso, AssignmentReference (por banco) manda mais que
                    # HouseBank: é o campo que a área usa pra marcar de propósito
                    # pra qual banco o título vai; HouseBank é só fallback.
                    b = bank_from_assignment(row["AssignmentReference"])
                    if b:
                        return b
                    return bank_from_housebank(row["HouseBank"])

                df["bank"] = df.apply(resolver_banco, axis=1)
                df["fidc"] = df["AssignmentReference"].apply(fidc_from_assignment)

                # carteira por FIDC — calculada ANTES de descartar as linhas sem
                # banco, porque um título de FIDC tem bank=None de propósito
                chave_titulo = ["AccountingDocument", "FiscalYear", "AccountingDocumentItem", "Customer"]
                for col in chave_titulo:
                    if col not in df.columns:
                        df[col] = None

                df_fidc = df[df["fidc"].notna()]
                if len(df_fidc):
                    grp_fidc_valor = df_fidc.groupby("fidc", as_index=False)["AmountInCompanyCodeCurrency"].sum()
                    for _, row in grp_fidc_valor.iterrows():
                        valor_por_fidc[row["fidc"]] = float(row["AmountInCompanyCodeCurrency"])
                    grp_fidc_qtd = df_fidc.drop_duplicates(subset=chave_titulo).groupby("fidc", as_index=False).size()
                    for _, row in grp_fidc_qtd.iterrows():
                        qtd_titulos_por_fidc[row["fidc"]] = int(row["size"])

                # carteira simples ABC — calculada ANTES de descartar as linhas
                # sem banco, mesma lógica do FIDC (bank=None de propósito pra
                # essas). É só uma observação de valor, não é uma "carteira" real.
                mask_simples_abc = df["AssignmentReference"].apply(is_carteira_simples_abc)
                valor_carteira_simples_abc = float(df.loc[mask_simples_abc, "AmountInCompanyCodeCurrency"].sum())
                qtd_carteira_simples_abc = int(
                    df[mask_simples_abc].drop_duplicates(subset=chave_titulo).shape[0]
                )

                df = df[df["bank"].notna()]
                grp = df.groupby(["Customer", "bank"], as_index=False)["AmountInCompanyCodeCurrency"].sum()
                for _, row in grp.iterrows():
                    saldo_normalizado.append({
                        "customer": str(row["Customer"]).strip(),
                        "bank": row["bank"],
                        "valor": float(row["AmountInCompanyCodeCurrency"]),
                    })

                # quantidade de títulos = qtd de linhas únicas pela chave do
                # documento + CLIENTE (AccountingDocument+FiscalYear+
                # AccountingDocumentItem+Customer) — confirmado direto no SAP que o
                # mesmo doc+item pode aparecer pra clientes diferentes (rateio entre
                # sacados), então sem o Customer a chave subestima a quantidade
                df_qtd = df.drop_duplicates(subset=chave_titulo)
                grp_qtd = df_qtd.groupby("bank", as_index=False).size()
                for _, row in grp_qtd.iterrows():
                    qtd_titulos_por_banco[row["bank"]] = int(row["size"])
        except Exception as e:
            erro_saldo = str(e)
            print(f"Erro ao buscar carteira no SAP: {e}")

        # ---- consulta 1b: VENCIMENTO DA CARTEIRA — mesmo filtro da consulta 1,
        # mas numa chamada SEPARADA porque incluir NetDueDate no $select faz o
        # SAP devolver o título quebrado em uma linha por parcela de vencimento
        # (cada uma com uma fração do valor). Isso é exatamente o que o gráfico
        # de aging e o "vencendo em 30 dias" precisam (somar as frações por
        # faixa/banco dá o valor certo), mas destruiria a soma "1 linha por
        # título" da consulta 1 se estivesse na mesma chamada — por isso os
        # totais de carteira (cards, elegibilidade) vêm só da consulta 1, e o
        # valor vencido apurado aqui é subtraído deles depois (ver
        # calcular_total_por_banco / totalByBank no front-end). ----
        try:
            registros = _sap_get(
                sap_url, sap_usuario, sap_senha,
                filtro="Customer ne '' and Customer ne '20012001' and Customer ne '40014001' and Customer ne '70017001' and Customer ne '80018001' and ClearingDate eq null and AccountingDocumentType eq 'RV' and TransactionCurrency eq 'BRL'",
                select="AccountingDocument,FiscalYear,AccountingDocumentItem,AmountInCompanyCodeCurrency,Customer,CustomerName,AssignmentReference,HouseBank,NetDueDate,PaymentMethod,PaymentTerms",
            )
            if registros:
                dfv = pd.DataFrame(registros)
                for col in ["AssignmentReference", "HouseBank", "NetDueDate", "PaymentMethod"]:
                    if col not in dfv.columns:
                        dfv[col] = None
                dfv["AmountInCompanyCodeCurrency"] = pd.to_numeric(
                    dfv["AmountInCompanyCodeCurrency"], errors="coerce"
                ).fillna(0.0)
                dfv["NetDueDate"] = dfv["NetDueDate"].apply(formatar_data_sap_iso)
                dfv = dfv[dfv["Customer"].astype(bool)]
                # forma de pagamento diferente de '' e 'E' — elimina a linha
                dfv = dfv[~dfv["PaymentMethod"].apply(is_excluded_payment_method)]

                def resolver_banco_v(row):
                    # mesma prioridade da consulta 1: referência excluída primeiro,
                    # depois FIDC, depois AssignmentReference antes de HouseBank.
                    # Aqui (só no gráfico de vencimento) "ABC" e "ABC-CV" ficam
                    # juntos numa única categoria "ABC" — a separação entre os
                    # dois só importa pra carteira/elegibilidade (consulta 1).
                    if is_excluded_reference(row["AssignmentReference"]):
                        return None
                    if fidc_from_assignment(row["AssignmentReference"]):
                        return None
                    b = bank_from_assignment(row["AssignmentReference"])
                    if b:
                        return b
                    if is_carteira_simples_abc(row["AssignmentReference"]):
                        return "ABC"
                    return bank_from_housebank(row["HouseBank"])

                dfv["bank"] = dfv.apply(resolver_banco_v, axis=1)
                dfv = dfv[dfv["bank"].notna()]

                def dias_ate_vencer(due_iso):
                    if not isinstance(due_iso, str):
                        return None
                    d = datetime.strptime(due_iso, "%Y-%m-%d").date()
                    return (d - hoje).days

                dfv["diasVencimento"] = dfv["NetDueDate"].apply(dias_ate_vencer)
                dfv["bucketVencimento"] = dfv["diasVencimento"].apply(
                    lambda d: bucket_por_dias(d) if d is not None else None
                )

                def soma_vencendo_ate(dias_limite):
                    sub = dfv[
                        dfv["diasVencimento"].notna() & (dfv["diasVencimento"] >= 0) & (dfv["diasVencimento"] <= dias_limite)
                        & (~dfv["AssignmentReference"].apply(is_carteira_simples_abc))
                    ]
                    return sub.groupby("bank", as_index=False)["AmountInCompanyCodeCurrency"].sum()

                for dias_limite, alvo in ((7, venc7_por_banco), (15, venc15_por_banco), (30, venc30_por_banco)):
                    grp = soma_vencendo_ate(dias_limite)
                    for _, row in grp.iterrows():
                        alvo[row["bank"]] = float(row["AmountInCompanyCodeCurrency"])

                # aging: valor em aberto por faixa de 30 em 30 dias até o
                # vencimento, por banco (gráfico "Vencimento da carteira")
                dfv_aging = dfv[dfv["bucketVencimento"].notna()]
                grp_aging = dfv_aging.groupby(
                    ["bucketVencimento", "bank"], as_index=False
                )["AmountInCompanyCodeCurrency"].sum()
                for _, row in grp_aging.iterrows():
                    aging_por_banco[row["bucketVencimento"]][row["bank"]] = float(
                        row["AmountInCompanyCodeCurrency"]
                    )

                # valor vencido por banco (buckets "Atrasado" + "Protestado"),
                # usado só pra ABATER da carteira da consulta 1 na exibição —
                # nunca é somado direto em nada. IMPORTANTE: "usado" (consulta 1)
                # é CV-only pro ABC (carteira simples fica de fora) — então o
                # vencido também precisa excluir carteira simples aqui, senão
                # abate um valor de um escopo diferente do que está sendo abatido.
                dfv_vencido = dfv[
                    dfv["diasVencimento"].notna() & (dfv["diasVencimento"] <= 0)
                    & (~dfv["AssignmentReference"].apply(is_carteira_simples_abc))
                ]
                grp_vencido = dfv_vencido.groupby("bank", as_index=False)["AmountInCompanyCodeCurrency"].sum()
                for _, row in grp_vencido.iterrows():
                    vencido_por_banco[row["bank"]] = float(row["AmountInCompanyCodeCurrency"])
        except Exception as e:
            erro_aging = str(e)
            print(f"Erro ao buscar vencimento da carteira no SAP: {e}")

        # ---- consulta 2: títulos em aberto sem banco (HouseBank vazio) —
        # usada só para listar o que falta decidir. Os que já têm
        # AssignmentReference apontando pra um dos 3 bancos já foram
        # contados na carteira acima (consulta 1) — aqui servem só para
        # informar "já direcionado", sem somar de novo. ----
        try:
            registros = _sap_get(
                sap_url, sap_usuario, sap_senha,
                filtro="Customer ne '' and Customer ne '20012001' and Customer ne '40014001' and Customer ne '70017001' and Customer ne '80018001' and ClearingDate eq null and AccountingDocumentType eq 'RV' and HouseBank eq '' and TransactionCurrency eq 'BRL'",
                select=("AccountingDocument,FiscalYear,AccountingDocumentItem,PostingDate,PaymentMethod,"
                        "AmountInCompanyCodeCurrency,NetDueDate,Customer,CustomerName,AssignmentReference,HouseBank,DocumentReferenceID_2,PaymentTerms"),
            )
            if registros:
                df = pd.DataFrame(registros)
                for col in ["AssignmentReference", "AccountingDocumentItem", "FiscalYear", "CustomerName", "PaymentMethod"]:
                    if col not in df.columns:
                        df[col] = None
                df["PostingDate"] = df["PostingDate"].apply(formatar_data_sap_iso)
                df["NetDueDate"] = df["NetDueDate"].apply(formatar_data_sap_iso)
                df["AmountInCompanyCodeCurrency"] = pd.to_numeric(
                    df["AmountInCompanyCodeCurrency"], errors="coerce"
                ).fillna(0.0)
                df = df[df["PostingDate"].notna() & df["Customer"].astype(bool)]

                for _, row in df.iterrows():
                    ref = row.get("AssignmentReference")
                    if fidc_from_assignment(ref):
                        # já tem destino certo (FIDC) — não entra na lista de decisão
                        continue
                    if is_excluded_reference(ref):
                        # referência de movimentação que não é de banco/FIDC
                        # (TED, encontro de NF, exportação, etc.) — não entra na lista
                        continue
                    if is_excluded_payment_method(row.get("PaymentMethod")):
                        # forma de pagamento diferente de '' e 'E' — não entra na lista
                        continue
                    itens_normalizados.append({
                        "referencia": str(row.get("DocumentReferenceID_2", "") or ""),
                        "doc": str(row.get("AccountingDocument", "") or ""),
                        "item": str(row.get("AccountingDocumentItem", "") or ""),
                        "postingDate": row["PostingDate"] if isinstance(row["PostingDate"], str) else None,
                        "dueDate": row["NetDueDate"] if isinstance(row["NetDueDate"], str) else None,
                        "amount": float(row["AmountInCompanyCodeCurrency"]),
                        "customer": str(row["Customer"]).strip(),
                        "customerName": str(row.get("CustomerName", "") or "").strip(),
                        "assignmentRef": str(ref) if ref else "",
                        "preAssignedBank": bank_from_assignment(ref),
                    })
        except Exception as e:
            erro_itens = str(e)
            print(f"Erro ao buscar títulos em aberto no SAP: {e}")
    else:
        print("Aviso: SAP_1_URL não configurada no arquivo de parâmetros.")

    return {
        "saldo": saldo_normalizado,
        "itens": itens_normalizados,
        "venc7PorBanco": venc7_por_banco,
        "venc15PorBanco": venc15_por_banco,
        "venc30PorBanco": venc30_por_banco,
        "qtdTitulosPorBanco": qtd_titulos_por_banco,
        "valorPorFidc": valor_por_fidc,
        "qtdTitulosPorFidc": qtd_titulos_por_fidc,
        "valorCarteiraSimplesAbc": valor_carteira_simples_abc,
        "qtdCarteiraSimplesAbc": qtd_carteira_simples_abc,
        "agingBuckets": aging_labels,
        "agingPorBanco": aging_por_banco,
        "vencidoPorBanco": vencido_por_banco,
        "erroSaldo": erro_saldo,
        "erroItens": erro_itens,
        "erroAging": erro_aging,
        "atualizadoEm": datetime.now().strftime("%d/%m/%Y %H:%M"),
    }


# ============================================================
# 5. CÁLCULO SERVER-SIDE (usado pela exportação CSV)
# ============================================================

def calcular_saldo_por_cliente_banco(dados):
    mapa = {}
    for r in dados["saldo"]:
        k = (r["customer"], r["bank"])
        mapa[k] = mapa.get(k, 0.0) + r["valor"]
    return mapa


def calcular_total_por_banco(dados):
    """Total por banco usado no roteamento (limite geral do banco). Abate o
    valor vencido (apurado numa consulta separada, ver obter_dados_recebiveis)
    do total bruto da carteira — nunca soma o vencido, só subtrai."""
    totais = {b: 0.0 for b in BANKS}
    for r in dados["saldo"]:
        totais[r["bank"]] = totais.get(r["bank"], 0.0) + r["valor"]
    vencido = dados.get("vencidoPorBanco", {})
    for b in BANKS:
        totais[b] = max(0.0, totais[b] - vencido.get(b, 0.0))
    return totais


def _referencia_key(item):
    """Chave de agrupamento das parcelas de um mesmo título: pela referência
    do documento (DocumentReferenceID_2) quando existir; senão o próprio
    doc+item vira um grupo de 1, pra não misturar títulos sem referência."""
    ref = item.get("referencia")
    if ref:
        return "REF:" + ref
    return "DOC:" + item["doc"] + ":" + item["item"]


def calcular_grupos_referencia(dados):
    """Agrupa os títulos ainda a decidir (sem banco pré-atribuído) pela
    referência do documento. Parcelas da mesma referência são avaliadas em
    conjunto: o prazo é medido pela MÉDIA de dias (lançamento -> vencimento)
    de todas as parcelas do grupo — uma parcela isolada acima do prazo
    máximo não desqualifica o grupo se a média ficar dentro do limite.
    Já o valor máx. por título e os limites acumulados (concentração /
    carteira, pela SOMA das parcelas do grupo) continuam contaminando o
    grupo inteiro se qualquer parcela ou a soma estourar."""
    grupos = {}
    for item in dados["itens"]:
        if item["preAssignedBank"]:
            continue
        grupos.setdefault(_referencia_key(item), []).append(item)
    return grupos


def _prazo_item(item):
    if isinstance(item["postingDate"], str) and isinstance(item["dueDate"], str):
        d1 = datetime.strptime(item["postingDate"], "%Y-%m-%d")
        d2 = datetime.strptime(item["dueDate"], "%Y-%m-%d")
        return (d2 - d1).days
    return None


def _grupo_prazo_medio(itens_grupo):
    """Média de dias até o vencimento entre as parcelas do grupo que têm
    data de lançamento e vencimento válidas."""
    prazos = [p for p in (_prazo_item(it) for it in itens_grupo) if p is not None]
    if not prazos:
        return None
    return sum(prazos) / len(prazos)


def _grupo_falha_individual(itens_grupo, banco):
    """True se alguma parcela do grupo, isoladamente, já estoura o valor
    máx. por título nesse banco — isso contamina o grupo inteiro. O prazo
    NÃO entra aqui: ele é avaliado pela média do grupo (_grupo_prazo_medio)."""
    r = BANK_RULES[banco]
    for it in itens_grupo:
        if r["valor_max_titulo"] is not None and it["amount"] > r["valor_max_titulo"]:
            return True
    return False


def avaliar_titulo(item, banco, saldo_map, totais, valor_acumulado=None, grupo_falha_individual=False, prazo_medio_grupo=None):
    """Elegibilidade = prazo (data) + concentração por sacado, apenas.
    O limite da carteira NÃO entra aqui — ele agora é um piso que a carteira
    deve alcançar (usado no cálculo de gap pra sugestão), não um teto que
    bloqueia elegibilidade."""
    r = BANK_RULES[banco]
    valor_acumulado = item["amount"] if valor_acumulado is None else valor_acumulado

    prazo = _prazo_item(item)
    # a regra de prazo usa a média das parcelas da mesma referência, não o
    # prazo individual do título — quando não há grupo (ou não há datas),
    # cai de volta pro prazo do próprio item
    prazo_regra = prazo_medio_grupo if prazo_medio_grupo is not None else prazo

    saldo_atual = saldo_map.get((item["customer"], banco), 0.0)
    novo_saldo_cliente = saldo_atual + valor_acumulado
    concentracao_resultante = (novo_saldo_cliente / r["limite_carteira"]) if r["limite_carteira"] else None

    ok = True
    if r["prazo_max"] is not None and prazo_regra is not None and prazo_regra > r["prazo_max"]:
        ok = False
    if r["concentracao"] is not None and r["limite_carteira"] is not None and concentracao_resultante > r["concentracao"]:
        ok = False
    if grupo_falha_individual:
        ok = False

    return ok, prazo


def calcular_gap_por_banco(totais, venc7_por_banco):
    """Gap de cada banco roteável = quanto falta pra alcançar o piso da
    carteira (limite_carteira, tratado como mínimo) + o que vence nos
    próximos 7 dias (esse valor vai sair da carteira em breve e precisa ser
    reposto). Banco com maior gap = maior prioridade na sugestão."""
    gaps = {}
    for b in ROUTABLE_BANKS:
        r = BANK_RULES[b]
        atual = totais.get(b, 0.0)
        falta_piso = max((r["limite_carteira"] or 0.0) - atual, 0.0) if r["limite_carteira"] is not None else 0.0
        vencendo7 = (venc7_por_banco or {}).get(b, 0.0)
        gaps[b] = falta_piso + vencendo7
    return gaps


def sugerir_banco(item, saldo_map, totais, venc7_por_banco, valor_acumulado=None, grupo_falhas=None, prazo_medio_grupo=None):
    grupo_falhas = grupo_falhas or {}
    gaps = calcular_gap_por_banco(totais, venc7_por_banco)
    elegiveis = []
    for b in ROUTABLE_BANKS:
        ok, _ = avaliar_titulo(item, b, saldo_map, totais, valor_acumulado, grupo_falhas.get(b, False), prazo_medio_grupo)
        if ok:
            elegiveis.append((b, gaps.get(b, 0.0)))
    if not elegiveis:
        return None, []
    elegiveis.sort(key=lambda x: -x[1])
    return elegiveis[0][0], [b for b, _ in elegiveis]


# ============================================================
# 4. EXTRAÇÃO DOS DADOS
# ============================================================

def obter_dados_cr():

    DEPARA_BANCOS = {
        "ABC": "ABC",
        "BB0": "BANCO DO BRASIL",
        "BB1": "BANCO DO BRASIL",
        "BB2": "BANCO DO BRASIL",
        "BB3": "BANCO DO BRASIL",
        "BB4": "BANCO DO BRASIL",
        "ITA": "ITAU",
        "SAN": "SANTANDER",
        "SFR": "SAFRA",
        "SOF": "SOFISA"
    }

    parametros        = carregar_parametros(
        os.environ.get(
            "DASHBOARD_PARAM_FILE",
            "/var/www/api_cobranca/parametros_prod.txt",
        )
    )
    SAP_API_1_URL     = parametros.get("SAP_1_URL", "")
    SAP_API_1_USUARIO = parametros.get("SAP_1_USUARIO", "")
    SAP_API_1_SENHA   = parametros.get("SAP_1_SENHA", "")

    # =========================================================
    # PASSO 1 — busca o SAP (títulos em aberto)
    #           guarda também um dict chave→{Dt_lcto,CreationTime}
    #           para cruzar com o CSV depois
    # =========================================================

    # Sem ClearingDate eq null: precisamos de todos os títulos (abertos e
    # liquidados) para cruzar com o CSV e calcular velocidade.
    filtro = (
        "Customer ne '' "
        "and AccountingDocumentType eq 'RV'"
        "and TransactionCurrency eq 'BRL'"
    )

    select = ",".join([
        "CompanyCode",
        "AccountingDocument",
        "FiscalYear",
        "AccountingDocumentItem",
        "AmountInCompanyCodeCurrency",
        "Dt_lcto",
        "CreationTime",
        "NetDueDate",
        "ClearingDate",
        "PostingKey",
        "Customer",
        "AssignmentReference",
        "HouseBank",
        "PaymentMethod",
        "PaymentTerms",
    ])

    params  = {"$filter": filtro, "$select": select, "$format": "json"}
    headers = {"Accept": "application/json"}

    raw_abertos  = []
    # chave "CC|DOC|ANO|ITEM" → {"Dt_lcto": "YYYY-MM-DD", "CreationTime": "HH:MM:SS"}
    sap_chave_map = {}
    df_abertos_bruto = None  # guardamos o dataframe por linha (sem agregar ainda)
    qtd_ajustar = 0  # títulos com lançamento == vencimento (D-1) — default se o SAP falhar

    try:
        if SAP_API_1_URL:
            response = requests.get(
                SAP_API_1_URL,
                params=params,
                headers=headers,
                auth=(SAP_API_1_USUARIO, SAP_API_1_SENHA),
                timeout=60,
            )
            response.raise_for_status()
            dados = response.json()

            if "d" in dados and "results" in dados["d"]:
                registros = dados["d"]["results"]
            elif "value" in dados:
                registros = dados["value"]
            else:
                registros = []

            if registros:
                df = pd.DataFrame(registros)

                for col in ["NetDueDate", "Dt_lcto", "AmountInCompanyCodeCurrency", "CreationTime", "AssignmentReference", "ClearingDate", "PostingKey", "PaymentMethod", "PaymentTerms"]:
                    if col not in df.columns:
                        df[col] = None

                # "Outras formas de pagamento": referência de movimentação que
                # não é título de banco/FIDC (TED, PIX, cartão, encontro de NF,
                # exportação, etc.) OU forma de pagamento diferente de '' e 'E'.
                # Antes essas linhas eram eliminadas; agora ficam no df só
                # marcadas — entram APENAS como volume empilhado nos gráficos
                # "Títulos a serem processados" e não influenciam mais nada
                # (KPIs, cobertura, a ajustar, velocidade). Ver PASSO 3.
                #
                # "Cedido a FIDC": AssignmentReference CONTÉM "FIDC" (qualquer
                # caixa). Tem prioridade sobre "outras formas": um título de
                # FIDC nunca cai em "outras", mesmo com PaymentMethod diferente.
                df["_fidc"] = (
                    df["AssignmentReference"].fillna("").astype(str)
                    .str.upper().str.contains("FIDC", regex=False)
                )
                df["_outra_forma"] = (
                    df["AssignmentReference"].apply(is_excluded_reference)
                    | df["PaymentMethod"].apply(is_excluded_payment_method)
                ) & ~df["_fidc"]

                df["NetDueDate"] = df["NetDueDate"].apply(formatar_data_sap_iso)
                df["Dt_lcto"]    = df["Dt_lcto"].apply(formatar_data_sap_iso)
                df["ClearingDate"] = df["ClearingDate"].apply(formatar_data_sap_iso)
                df["CreationTime"] = df["CreationTime"].apply(parsear_creation_time)

                CHAVE_API_1 = [
                    "CompanyCode", "AccountingDocument",
                    "FiscalYear", "AccountingDocumentItem",
                ]
                df = df.drop_duplicates(subset=CHAVE_API_1, keep="first")

                df["AmountInCompanyCodeCurrency"] = pd.to_numeric(
                    df["AmountInCompanyCodeCurrency"], errors="coerce"
                ).fillna(0.0)

                # ── monta dict de lookup chave → Dt_lcto + CreationTime ──────
                #    (sem "outras formas de pagamento" — velocidade não muda)
                for _, row in df[~df["_outra_forma"]].iterrows():
                    chave_sap = "|".join([
                        str(row.get("CompanyCode",             "")).strip(),
                        str(row.get("AccountingDocument",      "")).strip(),
                        str(row.get("FiscalYear",              "")).strip(),
                        str(row.get("AccountingDocumentItem",  "")).strip(),
                    ])
                    sap_chave_map[chave_sap] = {
                        "Dt_lcto":      row.get("Dt_lcto",      "") or "",
                        "CreationTime": row.get("CreationTime", "") or "",
                    }

                # guardamos o df por linha — a agregação por Dt_lcto só
                # acontece depois de sabermos quais chaves já foram
                # processadas (ver PASSO 2, logo abaixo)
                df_abertos_bruto = df

        else:
            print("Aviso: Credenciais/URL do SAP não encontradas.")

    except Exception as e:
        print(f"Erro ao buscar dados no SAP: {e}")

    # =========================================================
    # PASSO 2 — lê o CSV com separador fixo ','
    #           (cabeçalho: Chave_Titulo,HouseBank,Data_Processamento)
    #           e cruza com sap_chave_map para obter Dt_lcto+CreationTime
    #
    #           Precisa vir ANTES da agregação de abertos (passo 3),
    #           porque o gráfico de abertos usa o conjunto de chaves já
    #           processadas para marcar, por data, quantos títulos
    #           daquele dia já viraram remessa (mesmo que atrasados).
    # =========================================================

    processados_processed = []
    chaves_processadas = set()
    csv_path = os.environ.get(
        "DASHBOARD_CSV_PROCESSADOS",
        "/var/www/api_cobranca/titulos_processados.csv",
    )

    if os.path.exists(csv_path):
        try:
            # sep=',' explícito — evita inferência errada do engine="python"
            df_csv = pd.read_csv(
                csv_path,
                sep=",",
                dtype=str,
                encoding="utf-8-sig",   # lida com BOM gerado pelo to_csv(encoding="utf-8-sig")
            )
            df_csv.columns = df_csv.columns.str.strip()

            # garante as três colunas mínimas mesmo que o arquivo venha sem header
            cols_esperadas = ["Chave_Titulo", "HouseBank", "Data_Processamento"]
            if not all(c in df_csv.columns for c in cols_esperadas):
                # arquivo sem header — atribui nomes posicionalmente
                df_csv = pd.read_csv(
                    csv_path,
                    sep=",",
                    header=None,
                    names=cols_esperadas,
                    dtype=str,
                    encoding="utf-8-sig",
                )
                df_csv.columns = df_csv.columns.str.strip()

            chaves_processadas = set(
                df_csv["Chave_Titulo"].astype(str).str.strip()
            )

            for _, row in df_csv.iterrows():

                # ── Chave ─────────────────────────────────────────────────────
                chave = str(row.get("Chave_Titulo", "")).strip()

                # ── Banco ─────────────────────────────────────────────────────
                house_bank_raw = str(row.get("HouseBank", "")).strip()
                prefixo        = house_bank_raw[:3].upper()
                nome_banco     = DEPARA_BANCOS.get(prefixo, house_bank_raw or "OUTRO")

                # ── Data + hora de processamento ──────────────────────────────
                dt_raw  = str(row.get("Data_Processamento", "")).strip()
                partes  = dt_raw.split()
                data_iso = formatar_data_sap_iso(partes[0]) or "" if partes else ""
                hora_str = partes[1] if len(partes) > 1 else ""

                # ── Dt_lcto e CreationTime: vêm do SAP via chave ──────────────
                sap_info      = sap_chave_map.get(chave, {})
                dt_lcto_iso   = sap_info.get("Dt_lcto",      "")
                creation_time = sap_info.get("CreationTime", "")

                processados_processed.append({
                    "Data_Processamento": data_iso,
                    "Hora_Processamento": hora_str,
                    "HouseBank":          nome_banco,
                    "Doc_Num":            chave,
                    "Dt_lcto":            dt_lcto_iso,    # vem do SAP
                    "CreationTime":       creation_time,  # vem do SAP
                    "Valor":              0.0,
                })

        except Exception as e:
            print(f"Erro ao ler o CSV: {e}")

    # =========================================================
    # PASSO 3 — agrupamento por Dt_lcto para o gráfico de abertos.
    #           Cada título é marcado como "já processado" usando a
    #           MESMA chave (CompanyCode|AccountingDocument|FiscalYear|
    #           AccountingDocumentItem) usada para o cálculo de tempo
    #           de criação/processamento — mesmo que o processamento
    #           real tenha ocorrido depois (atrasado), ele conta na
    #           data original do título, não na data em que foi processado.
    # =========================================================

    raw_abertos = []
    if df_abertos_bruto is not None:
        df = df_abertos_bruto

        def _chave_da_linha(row):
            return "|".join([
                str(row.get("CompanyCode",            "")).strip(),
                str(row.get("AccountingDocument",     "")).strip(),
                str(row.get("FiscalYear",             "")).strip(),
                str(row.get("AccountingDocumentItem", "")).strip(),
            ])

        df["_chave"]      = df.apply(_chave_da_linha, axis=1)
        df["_processado"] = df["_chave"].isin(chaves_processadas)

        # ── Conciliados com adiantamento pela automação (log do
        #    ajustar_parcelas_clientes.py): saem dos "a emitir" e entram em
        #    "Outras formas de pagamento", mesmo já estando baixados no SAP. ──
        _conc_adt = carregar_conciliados_adiantamento()
        _chave_norm = (
            df["CompanyCode"].astype(str).str.strip() + "|"
            + df["AccountingDocument"].astype(str).str.strip() + "|"
            + df["FiscalYear"].astype(str).str.strip() + "|"
            + df["AccountingDocumentItem"].astype(str).str.strip().str.zfill(3)
        )
        mask_conc_adt = _chave_norm.isin(_conc_adt) & (df["AmountInCompanyCodeCurrency"] > 0)

        # ── "Títulos a serem ajustados": só entre os EM ABERTO de verdade
        #    (ainda não processados/enviados como remessa) — um título já
        #    processado não precisa de ajuste, mesmo que a data bata.
        #    Critério: data de criação (Dt_lcto) igual à data de vencimento
        #    (NetDueDate) — só a data, hora não entra na conta — olhando
        #    até D-1 (o que foi criado hoje ainda não entra na fila). ──────
        hoje_str = datetime.now().strftime("%Y-%m-%d")
        # Regra exata: ClearingDate = null (ainda em aberto de verdade) +
        # AccountingDocumentType = 'RV' (já garantido pelo filtro do SAP) +
        # NetDueDate = PostingDate (aqui, Dt_lcto) + PostingDate até ontem —
        # tudo isso POR CIMA dos filtros que já existem (Customer, exceções
        # de atribuição, etc.), aplicados mais acima neste mesmo df.
        mask_aberto = (df["ClearingDate"].isna() | (df["ClearingDate"] == ""))
        mask_datas_iguais = (
            df["Dt_lcto"].notna() & (df["Dt_lcto"] != "")
            & df["NetDueDate"].notna() & (df["NetDueDate"] != "")
            & (df["Dt_lcto"] == df["NetDueDate"])
        )
        # PostingKey = '01' — só entra na conta de "a ajustar" (critério extra,
        # pedido só pra essa métrica; não afeta a exclusão usada pelos gráficos
        # normais de "Títulos a serem processados"/"Títulos a Vencer" abaixo).
        mask_posting_key = df["PostingKey"].astype(str).str.strip() == "01"
        # PaymentTerms = 'A002' — também só pra essa métrica: "a ajustar"
        # considera apenas títulos com essa condição de pagamento.
        mask_payment_terms = df["PaymentTerms"].astype(str).str.strip().str.upper() == "A002"
        # "outras formas de pagamento" (PIX, cartão, TED...) não entram em
        # nenhuma métrica — só no empilhamento dos gráficos de volume
        mask_outra = df["_outra_forma"].fillna(False).astype(bool)
        mask_ajuste = (
            mask_aberto & mask_datas_iguais & mask_posting_key & mask_payment_terms
            & (df["Dt_lcto"] < hoje_str)
            & ~mask_outra
            & ~mask_conc_adt
        )
        qtd_ajustar = int(mask_ajuste.sum())

        # ── base usada pelos gráficos "Títulos a serem processados" e
        #    "Títulos a Vencer": só em aberto (ClearingDate null) e SEM os
        #    "a ajustar" (criação == vencimento) — esses já têm uma métrica
        #    própria e não devem inflar os gráficos normais de fluxo. ──────
        #    "Cedido a FIDC" sai de emitido/não emitido e vira categoria
        #    própria (mesma base: em aberto e sem o corte "a ajustar") — a
        #    altura total da barra continua a mesma, só muda a divisão.
        mask_fidc = df["_fidc"].fillna(False).astype(bool)
        df_geracao = df[mask_aberto & ~mask_datas_iguais & ~mask_outra & ~mask_fidc & ~mask_conc_adt]
        df_fidc    = df[mask_aberto & ~mask_datas_iguais & mask_fidc & ~mask_conc_adt]

        # ── "outras formas de pagamento" em aberto: só volume, empilhado
        #    como 3ª série nos gráficos (não somam em Count/Processado,
        #    então KPIs e cobertura continuam iguais). Sem o corte de
        #    criação == vencimento, porque PIX/cartão costumam ser à vista
        #    e esse corte existe só por causa da métrica "a ajustar". ──────
        df_outras = df[(mask_aberto & mask_outra) | mask_conc_adt]

        df_agrupado = (
            df_geracao.groupby("Dt_lcto")
            .agg(
                count=("AccountingDocument", "count"),
                processado=("_processado", "sum"),
                amount=("AmountInCompanyCodeCurrency", "sum"),
            )
            .reset_index()
        )
        outras_por_lcto = df_outras.groupby("Dt_lcto").size().to_dict()
        fidc_por_lcto   = df_fidc.groupby("Dt_lcto").size().to_dict()

        geracao_por_lcto = {row["Dt_lcto"]: row for _, row in df_agrupado.iterrows()}
        for dt in sorted(set(geracao_por_lcto) | set(outras_por_lcto) | set(fidc_por_lcto)):
            if not dt:
                continue
            row = geracao_por_lcto.get(dt)
            raw_abertos.append({
                "posting_date": dt,
                "count":        int(row["count"])      if row is not None else 0,
                "processado":   int(row["processado"]) if row is not None else 0,
                "amount":       float(row["amount"])   if row is not None else 0.0,
                "outras":       int(outras_por_lcto.get(dt, 0)),
                "fidc":         int(fidc_por_lcto.get(dt, 0)),
            })

    # ── deslocamento +1 dia útil ──────────────────────────────
    abertos_processed = []
    for r in raw_abertos:
        dt        = r["posting_date"]
        target_dt = proximo_dia_util(dt)
        abertos_processed.append({
            "Date":          target_dt,
            "Original_Date": dt,
            "Count":         r["count"],
            "Processado":    r["processado"],
            "Outras":        r["outras"],
            "Fidc":          r["fidc"],
            "Amount":        r["amount"],
            "Deslocado":     dt != target_dt,
        })

    # =========================================================
    # PASSO 3B — mesmo raciocínio do "Títulos a serem processados",
    #            mas agrupado por NetDueDate (vencimento) em vez de
    #            Dt_lcto+1DU: dos títulos que vencem em cada dia,
    #            quantos já foram emitidos (mesma flag _processado).
    #            Sem deslocamento de dia útil — a data de vencimento
    #            já é a data-alvo, não precisa ajustar.
    # =========================================================
    vencimento_processed = []
    if df_abertos_bruto is not None:
        df_venc = df_geracao[df_geracao["NetDueDate"].notna() & (df_geracao["NetDueDate"] != "")]
        df_venc_agrupado = (
            df_venc.groupby("NetDueDate")
            .agg(
                count=("AccountingDocument", "count"),
                processado=("_processado", "sum"),
                amount=("AmountInCompanyCodeCurrency", "sum"),
            )
            .reset_index()
        )
        df_outras_venc = df_outras[df_outras["NetDueDate"].notna() & (df_outras["NetDueDate"] != "")]
        outras_por_venc = df_outras_venc.groupby("NetDueDate").size().to_dict()
        df_fidc_venc    = df_fidc[df_fidc["NetDueDate"].notna() & (df_fidc["NetDueDate"] != "")]
        fidc_por_venc   = df_fidc_venc.groupby("NetDueDate").size().to_dict()

        geracao_por_venc = {row["NetDueDate"]: row for _, row in df_venc_agrupado.iterrows()}
        for dt in sorted(set(geracao_por_venc) | set(outras_por_venc) | set(fidc_por_venc)):
            if not dt:
                continue
            row = geracao_por_venc.get(dt)
            vencimento_processed.append({
                "Date":       dt,
                "Count":      int(row["count"])      if row is not None else 0,
                "Processado": int(row["processado"]) if row is not None else 0,
                "Amount":     float(row["amount"])   if row is not None else 0.0,
                "Outras":     int(outras_por_venc.get(dt, 0)),
                "Fidc":       int(fidc_por_venc.get(dt, 0)),
            })

    # =========================================================
    # PASSO 3 — CSV histórico (antes da automação)
    #   Formato: Data de;Delta de criação
    #   Data: DD/MM/YYYY  |  Delta: float em dias (vírgula decimal)
    #   Converte delta de dias → horas para comparar com os processados
    # =========================================================

    historico_processed = []
    csv_hist = os.environ.get(
        "DASHBOARD_CSV_HISTORICO",
        "/var/www/api_cobranca/historico_velocidade.csv",
    )

    if os.path.exists(csv_hist):
        try:
            df_hist = pd.read_csv(
                csv_hist,
                sep=";",
                dtype=str,
                encoding="utf-8-sig",
            )
            df_hist.columns = df_hist.columns.str.strip()

            # normaliza nomes de colunas independente de maiúsculas/acentos
            col_data  = next((c for c in df_hist.columns if "data" in c.lower()), None)
            col_delta = next((c for c in df_hist.columns if "delta" in c.lower()), None)

            if col_data and col_delta:
                for _, row in df_hist.iterrows():
                    dt_raw    = str(row.get(col_data,  "")).strip()
                    delta_raw = str(row.get(col_delta, "")).strip().replace(",", ".")

                    # data DD/MM/YYYY → YYYY-MM-DD
                    data_iso = formatar_data_sap_iso(dt_raw)
                    if not data_iso:
                        continue

                    try:
                        delta_dias  = float(delta_raw)
                        delta_horas = round(delta_dias * 24, 2)
                    except ValueError:
                        continue

                    historico_processed.append({
                        "Data_Processamento": data_iso,
                        "Delta_h":            delta_horas,
                    })
            else:
                print(f"[AVISO] Colunas não encontradas em {csv_hist}: {list(df_hist.columns)}")

        except Exception as e:
            print(f"Erro ao ler histórico: {e}")

    return abertos_processed, processados_processed, historico_processed, qtd_ajustar, vencimento_processed


# ============================================================
# 4B. LOG DIÁRIO — Títulos a Ajustar
# ============================================================
# Grava a quantidade de "títulos a ajustar" só na PRIMEIRA vez que o app é
# aberto no dia (não sobrescreve se alguém abrir de novo mais tarde) — o
# gráfico de histórico usa esse snapshot do início do dia, não o valor
# ao vivo (que fica só na caixa de KPI, recalculado a cada carregamento).

AJUSTE_LOG_PATH = os.environ.get("AJUSTE_LOG_PATH", "/var/www/api_cobranca/ajuste_titulos_log.csv")


def registrar_e_carregar_historico_ajuste(qtd_hoje):
    hoje = datetime.now().strftime("%Y-%m-%d")
    linhas = []

    if os.path.exists(AJUSTE_LOG_PATH):
        try:
            with open(AJUSTE_LOG_PATH, "r", encoding="utf-8") as f:
                for row in csv.DictReader(f):
                    data = (row.get("data") or "").strip()
                    qtd = row.get("quantidade")
                    if data and qtd not in (None, ""):
                        linhas.append({"data": data, "quantidade": int(float(qtd))})
        except Exception as e:
            print(f"Erro ao ler {AJUSTE_LOG_PATH}: {e}")

    ja_tem_hoje = any(l["data"] == hoje for l in linhas)
    if not ja_tem_hoje:
        linhas.append({"data": hoje, "quantidade": int(qtd_hoje)})
        try:
            linhas.sort(key=lambda l: l["data"])
            with open(AJUSTE_LOG_PATH, "w", encoding="utf-8", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=["data", "quantidade"])
                writer.writeheader()
                writer.writerows(linhas)
        except Exception as e:
            print(f"Erro ao gravar {AJUSTE_LOG_PATH}: {e}")

    linhas.sort(key=lambda l: l["data"])
    return linhas


# ============================================================
# 4C. TÍTULOS AJUSTADOS / CONCILIADOS PELA AUTOMAÇÃO
# ============================================================
# Gerado pelo ajustar_parcelas_clientes.py (data,ajustados,conciliados): quantos títulos o
# programa reparcelou e quantas compensações com adiantamento/pagamento fez em cada dia.
# Cada coluna vira uma linha no gráfico de histórico. Arquivo no formato antigo
# (data,quantidade) continua sendo lido: a quantidade conta como "ajustados".

TITULOS_AJUSTADOS_PATH = os.environ.get(
    "TITULOS_AJUSTADOS_PATH", "/var/www/api_cobranca/titulos_ajustados.csv")


# Log gerado pelo ajustar_parcelas_clientes.py: uma linha por item de título que a
# automação baixou com adiantamento/pagamento (data,Chave_Titulo,adiantamento,valor).
# Só vale daqui pra frente (sem histórico): o que está nesse log sai dos "a emitir"
# e passa para "Outras formas de pagamento".
TITULOS_CONCILIADOS_ADT_PATH = os.environ.get(
    "TITULOS_CONCILIADOS_ADT_PATH",
    "/var/www/api_cobranca/titulos_conciliados_adiantamento.csv")


def carregar_conciliados_adiantamento():
    """{"Empresa|Documento|Ano|Item"} (item com 3 dígitos) do log de conciliações."""
    chaves = set()
    if not os.path.exists(TITULOS_CONCILIADOS_ADT_PATH):
        return chaves
    try:
        with open(TITULOS_CONCILIADOS_ADT_PATH, "r", encoding="utf-8-sig") as f:
            for row in csv.DictReader(f):
                partes = [p.strip() for p in str(row.get("Chave_Titulo") or "").split("|")]
                if len(partes) == 4 and all(partes):
                    partes[3] = partes[3].zfill(3)
                    chaves.add("|".join(partes))
    except Exception as e:
        print(f"Erro ao ler {TITULOS_CONCILIADOS_ADT_PATH}: {e}")
    return chaves


def carregar_titulos_ajustados():
    """{data: {"ajustados": n, "conciliados": n}}"""
    dados = {}
    if not os.path.exists(TITULOS_AJUSTADOS_PATH):
        return dados
    try:
        with open(TITULOS_AJUSTADOS_PATH, "r", encoding="utf-8-sig") as f:
            for row in csv.DictReader(f):
                data = (row.get("data") or "").strip()
                if not data:
                    continue
                aj = row.get("ajustados")
                if aj in (None, ""):
                    aj = row.get("quantidade")          # formato antigo
                d = dados.setdefault(data, {"ajustados": 0, "conciliados": 0})
                d["ajustados"] += int(float(aj or 0))
                d["conciliados"] += int(float(row.get("conciliados") or 0))
    except Exception as e:
        print(f"Erro ao ler {TITULOS_AJUSTADOS_PATH}: {e}")
    return dados


def juntar_historico_ajuste(historico_ajuste, automacao):
    """Une as séries por data. Dia sem registro numa delas fica None (sem ponto)."""
    a_ajustar = {l["data"]: l["quantidade"] for l in historico_ajuste}
    return [{"data": d,
             "quantidade": a_ajustar.get(d),
             "ajustados": (automacao.get(d) or {}).get("ajustados"),
             "conciliados": (automacao.get(d) or {}).get("conciliados")}
            for d in sorted(set(a_ajustar) | set(automacao))]


# ============================================================
# 5. DASHBOARD
# ============================================================

@app.route("/dashboard-cr")
def dashboard_cr():
    try:
        abertos, processados, historico, qtd_ajustar, vencimento = obter_dados_cr()
    except Exception as e:
        return (
            f"<h1 style='color:white;background:#0c1810;padding:20px;'>"
            f"Erro ao carregar dados: {e}</h1>"
        )

    historico_ajuste = juntar_historico_ajuste(
        registrar_e_carregar_historico_ajuste(qtd_ajustar),
        carregar_titulos_ajustados())

    abertos_json     = json.dumps(abertos,     ensure_ascii=False)
    processados_json = json.dumps(processados, ensure_ascii=False)
    historico_json   = json.dumps(historico,   ensure_ascii=False)
    historico_ajuste_json = json.dumps(historico_ajuste, ensure_ascii=False)
    vencimento_json  = json.dumps(vencimento,  ensure_ascii=False)


    # ---- aba "Títulos nos bancos" ----
    global BANK_RULES
    BANK_RULES = carregar_regras_bancos()
    dados_tit = obter_dados_recebiveis()

    html_template = r"""
<!DOCTYPE html>
<html lang="pt-BR">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Contas a Receber</title>
<link href="https://fonts.googleapis.com/css2?family=Playfair+Display:wght@400;600&family=JetBrains+Mono:wght@300;400;600;700&display=swap" rel="stylesheet">
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.3/dist/chart.umd.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/chartjs-plugin-datalabels@2.2.0"></script>
<style>
*{margin:0;padding:0;box-sizing:border-box}
:root{
  --bg:#0c1810;--sur:#101e14;--card:#141f17;--card2:#192419;
  --bdr:#1d2f20;--bdr2:#253828;
  --G:#3ecf6e;--A:#f5a623;--R:#f06060;--B:#5ba4cf;--O:#eda100;--V:#a78bfa;
  --t:#dff0e4;--t2:#8aab94;--t3:#4a6652;
  --FH:'Playfair Display',Georgia,serif;
  --FM:'JetBrains Mono',monospace;
}
body{background:var(--bg);color:var(--t);font-family:var(--FM);font-size:12px;overflow-x:hidden;padding-bottom:40px}
::-webkit-scrollbar{width:6px;height:6px}
::-webkit-scrollbar-track{background:var(--bg)}
::-webkit-scrollbar-thumb{background:var(--t3);border-radius:3px}

/* ── header ── */
#hdr{display:flex;align-items:center;justify-content:space-between;padding:16px 28px;
  border-bottom:1px solid var(--bdr);background:linear-gradient(90deg,#0c1810,#0f2015 50%,#0c1810);
  position:sticky;top:0;z-index:300;gap:20px;flex-wrap:wrap}
.brand-wrap{display:flex;align-items:center;gap:16px}
.brand-logo{height:48px;width:auto;object-fit:contain;filter:drop-shadow(0px 2px 4px rgba(0,0,0,.5));
  background-color:#fff;padding:4px;border-radius:6px}
.brand h1{font-family:var(--FH);font-size:1.4rem;font-weight:600;color:var(--G);margin-bottom:2px}
.brand p{font-size:10px;color:var(--t2);letter-spacing:1px;text-transform:uppercase}
.filters{display:flex;align-items:center;gap:12px;background:var(--card2);padding:8px 16px;
  border-radius:6px;border:1px solid var(--bdr2);flex-wrap:wrap;margin-left:auto}
.flbl{font-size:10px;color:var(--t2);text-transform:uppercase;letter-spacing:1px}
input[type="date"]{background:var(--sur);border:1px solid var(--bdr2);color:var(--t);
  font-family:var(--FM);font-size:11px;padding:4px 8px;border-radius:4px;outline:none;color-scheme:dark}
.btn{background:var(--G);color:#0c1810;border:none;padding:6px 12px;font-family:var(--FM);
  font-weight:700;font-size:10px;border-radius:4px;cursor:pointer;text-transform:uppercase;
  transition:.2s;text-decoration:none;display:inline-flex;align-items:center;gap:4px}
.btn:hover{background:var(--t)}
.btn-excel{background:#1f7244;color:#fff}
.btn-excel:hover{background:#288c54;color:#fff}
.btn-reset{background:transparent;color:var(--t2);border:1px solid var(--bdr2)}
.btn-reset:hover{border-color:var(--t);color:var(--t);background:transparent}

/* ── layout ── */
#main{padding:24px 28px}
.ks{display:grid;grid-template-columns:repeat(4,1fr);gap:16px;margin-bottom:20px}
.ks3{display:grid;grid-template-columns:repeat(3,1fr);gap:16px;margin-bottom:16px}
.kp{background:var(--card);border:1px solid var(--bdr);border-radius:6px;padding:18px;
  position:relative;overflow:hidden;transition:transform .2s,border-color .2s}
.kp:hover{transform:translateY(-2px);border-color:var(--bdr2)}
.kp::before{content:'';position:absolute;top:0;left:0;right:0;height:3px;
  background:linear-gradient(90deg,transparent,var(--kc,var(--G)),transparent)}
.kp.a{--kc:var(--A)}.kp.r{--kc:var(--R)}.kp.b{--kc:var(--B)}.kp.g{--kc:var(--G)}.kp.v{--kc:var(--V)}
.kl{font-size:10px;letter-spacing:1.2px;text-transform:uppercase;color:var(--t2);margin-bottom:8px}
.kv{font-family:var(--FH);font-size:2.2rem;font-weight:600;color:var(--t);line-height:1}
.ksub{font-size:10px;color:var(--t3);margin-top:6px}
.g12{display:grid;grid-template-columns:1fr 2fr;gap:16px;margin-bottom:16px}
.c{background:var(--card);border:1px solid var(--bdr);border-radius:6px;padding:18px;margin-bottom:16px}
.ct{font-size:11px;letter-spacing:1px;text-transform:uppercase;color:var(--t);margin-bottom:4px;
  display:flex;align-items:center;gap:8px;font-weight:600}
.ct-sub{font-size:10px;color:var(--t2);margin-bottom:16px;margin-left:12px}
.ct::before{content:'';width:4px;height:12px;background:var(--G);border-radius:2px}
.c.b .ct::before{background:var(--B)}
.c.g .ct::before{background:var(--G)}
.c.v .ct::before{background:var(--V)}
.c.a .ct::before{background:var(--A)}
.cw{position:relative;width:100%}
.vt{width:100%;border-collapse:collapse;font-size:11px}
.vt th{font-size:9px;text-transform:uppercase;color:var(--t2);padding:8px;text-align:left;
  border-bottom:1px solid var(--bdr2);background:var(--card2)}
.vt td{padding:8px;border-bottom:1px solid var(--bdr);color:var(--t)}
.vt tfoot td{padding:8px;border-top:1px solid var(--bdr2);background:var(--card2)}
.vt tr:hover{background:rgba(255,255,255,.02)}
.gauge-wrap{display:flex;flex-direction:column;align-items:center;justify-content:center;height:100%;min-height:140px}
.gauge-val{font-family:var(--FH);font-size:3rem;color:var(--t);margin-bottom:4px}
.gauge-lbl{color:var(--t2);text-transform:uppercase;letter-spacing:1px;font-size:10px}
.bar-bg{width:80px;height:6px;background:var(--bdr2);border-radius:3px;overflow:hidden;display:inline-block;vertical-align:middle;margin-right:8px}
.bar-fg{height:100%;border-radius:3px}
/* badge velocidade */
.badge{border-radius:4px;padding:2px 8px;font-size:11px;font-weight:600;display:inline-block}
.badge-g{background:#dcfce7;color:#15803d}
.badge-a{background:#fef9c3;color:#a16207}
.badge-r{background:#fee2e2;color:#b91c1c}
/* div separadora de seção */
.section-sep{margin:8px 0 20px;padding:10px 0 4px;border-top:1px solid var(--bdr2);
  font-size:10px;letter-spacing:2px;text-transform:uppercase;color:var(--t3)}
/* abas */
.tabs{display:flex;gap:4px;margin-bottom:20px;border-bottom:1px solid var(--bdr2)}
.tab-btn{background:transparent;border:none;color:var(--t2);font-family:var(--FM);
  font-size:11px;font-weight:600;letter-spacing:1px;text-transform:uppercase;
  padding:12px 20px;cursor:pointer;border-bottom:2px solid transparent;transition:.2s}
.tab-btn:hover{color:var(--t)}
.tab-btn.active{color:var(--G);border-bottom-color:var(--G)}
.tab-content{display:none}
.tab-content.active{display:block}

/* ============================================================
   ABA "TÍTULOS NOS BANCOS" — regras adicionais (não duplicam nada
   já definido acima; onde precisou de um valor diferente, foi
   escopado só pra dentro da aba via #tab-titulos)
   ============================================================ */
input[type="text"],input[type="number"],select{background:var(--sur);border:1px solid var(--bdr2);color:var(--t);
  font-family:var(--FM);font-size:11px;padding:4px 8px;border-radius:4px;outline:none;color-scheme:dark}
#tab-titulos .kv{font-size:1.8rem}
.ks2{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,420px));gap:16px;margin-bottom:8px}
.ks-combined{display:grid;grid-template-columns:repeat(3,1fr);gap:16px;margin-bottom:8px}
.ks-combined .spacer{visibility:hidden}
@media(max-width:820px){.ks-combined{grid-template-columns:1fr}.ks-combined .spacer{display:none}}
.kp .refnote{margin-top:10px;padding-top:8px;border-top:1px dashed var(--bdr2);font-size:10px;color:var(--t3)}
.kp .refnote b{color:var(--V);font-family:var(--FM)}
.bar-fg.under{background:var(--R)}
.bar-fg.near{background:var(--A)}
.bar-fg.over{background:var(--G)}
.bar-fg-venc{position:absolute;left:0;top:0;bottom:0;
  background:repeating-linear-gradient(45deg,rgba(0,0,0,.4),rgba(0,0,0,.4) 4px,rgba(0,0,0,.15) 4px,rgba(0,0,0,.15) 8px)}
#tab-titulos .bar-fg{position:relative;overflow:hidden}

.filters2{display:flex;flex-wrap:wrap;gap:14px;align-items:flex-end}
.field{display:flex;flex-direction:column;gap:4px}
.field label{font-size:9.5px;text-transform:uppercase;letter-spacing:.5px;color:var(--t2)}
.field.grow{flex:1;min-width:160px}
.checkfield{display:flex;align-items:center;gap:6px;padding-bottom:7px}
.checkfield label{font-size:11px;color:var(--t2)}

.summary-strip{display:flex;gap:24px;flex-wrap:wrap;margin:2px 2px 14px;font-size:11px;color:var(--t2)}
.summary-strip b{font-family:var(--FH);color:var(--t);font-size:15px;display:block}

.rtb-wrap{overflow-x:auto}
.rt-wrap{overflow-x:auto}
table.rules{width:100%;border-collapse:collapse;min-width:640px}
table.rules th{font-size:9px;text-transform:uppercase;color:var(--t2);padding:8px;text-align:left;
  border-bottom:1px solid var(--bdr2);background:var(--card2)}
table.rules td{padding:6px 8px;border-bottom:1px solid var(--bdr)}
table.rules tr:last-child td{border-bottom:none}
table.rules td.bankcell{font-family:var(--FH);font-weight:600;font-size:14px;color:var(--t);white-space:nowrap}
table.rules input{width:100%;min-width:100px;font-family:var(--FM);font-size:11.5px;
  border:1px solid var(--bdr2);border-radius:4px;padding:5px 7px;background:var(--sur);color:var(--t)}
table.rules input:focus{outline:1px solid var(--G);outline-offset:-1px}
table.rtb{width:100%;border-collapse:collapse;font-size:11px;min-width:920px}
table.rtb th{font-size:9px;text-transform:uppercase;color:var(--t2);padding:9px 10px;text-align:left;
  border-bottom:1px solid var(--bdr2);background:var(--card2);white-space:nowrap}
table.rtb td{padding:9px 10px;border-bottom:1px solid var(--bdr);color:var(--t)}
table.rtb td.num{font-family:var(--FM);text-align:right;white-space:nowrap}
table.rtb tr:hover{background:rgba(255,255,255,.02)}
table.rtb tr.datarow:hover{cursor:pointer}
.chip{display:inline-block;font-size:10px;padding:2px 7px;border-radius:4px;margin:1px 3px 1px 0;font-weight:600}
.chip.ok{background:rgba(62,207,110,.15);color:var(--G)}
.chip.bad{background:rgba(240,96,96,.15);color:var(--R)}
.suggest-pill{font-family:var(--FH);font-weight:700;font-size:13px;color:var(--G)}
.suggest-pill.none{font-family:var(--FM);font-weight:600;font-size:10.5px;color:var(--R);text-transform:uppercase}
.detail-row td{background:var(--card2);padding:14px 20px}
.detail-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:14px}
.detail-bank{border:1px solid var(--bdr2);border-radius:5px;padding:10px 12px;background:var(--card)}
.detail-bank .h{font-family:var(--FH);font-weight:600;font-size:13.5px;margin-bottom:6px;display:flex;justify-content:space-between}
.detail-bank .crit{display:flex;justify-content:space-between;font-size:10.5px;padding:2px 0;color:var(--t2)}
.detail-bank .crit.fail{color:var(--R)}
.detail-bank .crit .v{font-family:var(--FM)}
.sub-table{width:100%;border-collapse:collapse;font-size:10.5px;margin-top:4px}
.sub-table th{font-size:9px;text-transform:uppercase;color:var(--t2);padding:4px 8px;text-align:left;border-bottom:1px solid var(--bdr2)}
.sub-table td{padding:4px 8px;border-bottom:1px solid var(--bdr);color:var(--t2);font-family:var(--FM)}
.sub-table tr:last-child td{border-bottom:none}
.empty{text-align:center;padding:48px 20px;color:var(--t3);border:1px dashed var(--bdr2);border-radius:6px}
.empty b{color:var(--t2)}
.toolbar{display:flex;justify-content:flex-end;margin-bottom:10px;gap:8px}
.warn-box{background:rgba(240,96,96,.1);border:1px solid var(--R);color:var(--R);padding:10px 14px;
  border-radius:6px;font-size:11px;margin-bottom:16px}

/* gráfico de aging (vencimento da carteira) */
.chart-toolbar{display:flex;align-items:center;justify-content:space-between;gap:12px;margin-bottom:14px;flex-wrap:wrap}
.legend{display:flex;gap:16px;flex-wrap:wrap}
.legend-item{display:flex;align-items:center;gap:6px;font-size:10.5px;color:var(--t2)}
.legend-swatch{width:10px;height:10px;border-radius:2px;flex:none}
.aging-svg{width:100%;height:auto;overflow:visible}
.aging-grid{stroke:var(--bdr2);stroke-width:1}
.aging-axis-label{fill:var(--t3);font-family:var(--FM);font-size:9.5px}
.aging-bar{cursor:pointer;transition:opacity .15s}
.aging-bar:hover{opacity:.75}
.aging-tip{position:fixed;pointer-events:none;background:var(--card2);border:1px solid var(--bdr2);
  border-radius:5px;padding:6px 10px;font-size:10.5px;color:var(--t);z-index:400;box-shadow:0 4px 14px rgba(0,0,0,.4)}
.aging-tip .val{font-family:var(--FM);font-weight:700;color:var(--t)}
.aging-tip .lbl{color:var(--t2)}
table.aging{width:100%;border-collapse:collapse;font-size:11px}
table.aging th{font-size:9px;text-transform:uppercase;color:var(--t2);padding:8px;text-align:right;
  border-bottom:1px solid var(--bdr2);background:var(--card2)}
table.aging th:first-child{text-align:left}
table.aging td{padding:6px 8px;border-bottom:1px solid var(--bdr);text-align:right;font-family:var(--FM);color:var(--t)}
table.aging td:first-child{text-align:left;font-family:var(--FM);color:var(--t2)}
table.aging tr:last-child td{border-bottom:none;font-weight:700}
footer.note{margin-top:36px;padding-top:14px;border-top:1px solid var(--bdr);font-size:10px;color:var(--t3);line-height:1.6}
/* correções escopadas só pra dentro da aba "Títulos nos bancos" — o
   dashboard já define .kl e .bar-bg de um jeito compatível com o uso
   dele (texto simples / barra fixa de 80px), diferente do que nossos
   cards precisam (duas pontas separadas / barra ocupando a largura toda) */
#tab-titulos .kl{display:flex;justify-content:space-between;align-items:center}
#tab-titulos .bar-bg{width:100%;display:block;margin-right:0;vertical-align:initial}
#tab-titulos .section-sep{margin:28px 0 16px}
#fidcCards{grid-template-columns:repeat(auto-fit,minmax(220px,1fr))}
</style>
</head>
<body>

<div id="hdr">
  <div class="brand-wrap">
    
    <div class="brand">
      <h1>Contas a Receber</h1>
      <p>Gestão de Remessas Bancárias &amp; Velocidade de Processamento</p>
    </div>
  </div>
  <div class="filters">
    <span class="flbl">Período:</span>
    <input type="date" id="dtStart">
    <span style="color:var(--t3)">até</span>
    <input type="date" id="dtEnd">
    <button class="btn" onclick="applyFilter()">Analisar</button>
    <button class="btn btn-reset" onclick="resetFilter()">Todos os Dados</button>
  </div>
</div>

<div id="main">

  <div class="tabs">
    <button class="tab-btn active" id="tabBtn-titulos"    onclick="showTab('titulos')">Títulos nos bancos</button>
    <button class="tab-btn"        id="tabBtn-geracao"    onclick="showTab('geracao')">Geração de Títulos</button>
    <button class="tab-btn"        id="tabBtn-velocidade" onclick="showTab('velocidade')">Velocidade de Processamento</button>
    <button class="tab-btn"        id="tabBtn-exclusao"   onclick="showTab('exclusao')">Exclusão de Títulos</button>
  </div>

<div id="tab-titulos" class="tab-content active">

{% if erro_saldo %}<div class="warn-box">Erro ao buscar saldo por cliente/banco no SAP: {{ erro_saldo }}</div>{% endif %}
  {% if erro_itens %}<div class="warn-box">Erro ao buscar títulos em aberto no SAP: {{ erro_itens }}</div>{% endif %}
  {% if erro_aging %}<div class="warn-box">Erro ao buscar vencimento da carteira no SAP: {{ erro_aging }}</div>{% endif %}

  <div class="section-sep">Carteira por banco · atualizado em {{ atualizado_em_tit }}</div>
  <div class="ks-combined" id="overviewCards"></div>

  <div class="section-sep">Carteira por FIDC</div>
  <div class="ks3" id="fidcCards"></div>
  <div class="section-sep">Vencimento da carteira por banco</div>
  <div class="c">
    <div class="ct">Valor em aberto por faixa de vencimento</div>
    <div class="ct-sub">Considera a data de vencimento (NetDueDate) de cada título já na carteira, em faixas de 30 dias.</div>
    <div class="chart-toolbar">
      <div class="legend" id="agingLegend"></div>
      <button class="btn btn-reset" id="agingToggleView" type="button">Ver como tabela</button>
    </div>
    <div id="agingChartWrap"></div>
    <div id="agingTableWrap" style="display:none"></div>
  </div>

  <div class="section-sep">Regras dos bancos</div>
  <div class="c">
    <div class="ct">Parâmetros por banco</div>
    <div class="ct-sub">Editável — clique em "Salvar regras" para gravar de forma definitiva (grava no arquivo <code>/var/www/api_cobranca/regras_bancos.csv</code> usado pelo sistema, vale para todo mundo que abrir a página depois).</div>
    <div class="rt-wrap"><table class="rules" id="rulesTable"></table></div>
    <div style="display:flex;gap:12px;align-items:center;margin-top:14px">
      <button class="btn" id="saveRulesBtn" type="button">Salvar regras</button>
      <span id="saveRulesStatus" style="font-size:11.5px;color:var(--t2)"></span>
    </div>
  </div>

  <div class="section-sep">Filtrar</div>
  <div class="c">
    <div class="filters2">
      <div class="field"><label>Lançamento de</label><input type="date" id="fDateFrom"></div>
      <div class="field"><label>até</label><input type="date" id="fDateTo"></div>
      <div class="field"><label>Banco</label><select id="fBank"><option value="">Todos os bancos</option></select></div>
      <div class="checkfield"><input type="checkbox" id="fOnlyEligible"><label for="fOnlyEligible">Apenas com sugestão</label></div>
      <div style="margin-left:auto;display:flex;gap:8px">
        <a href="/dashboard-cr" class="btn btn-reset">↻ Atualizar</a>
        <a href="#" id="exportServerBtn" class="btn btn-excel">⇩ Exportar CSV</a>
      </div>
    </div>
  </div>

  <div class="section-sep">Resultado</div>
  <div class="summary-strip" id="summaryStrip"></div>
  <div id="resultsHolder"></div>

  <footer class="note">
    Saldo e títulos em aberto vêm direto do SAP a cada carregamento da página (botão "Atualizar").<br>
    De/para de banco fixo no código a partir do prefixo do HouseBank (ABC → ABC, SFR → Safra, SOF → Sofisa).<br>
    Títulos cuja Referência de Atribuição já contém "ABC", "SAFRA" ou "SOFISA" são somados à carteira utilizada do banco, mas não entram na lista de decisão.
  </footer>
</div>

</div>

  <div id="tab-geracao" class="tab-content">

  <!-- ── KPIs Volume ── -->
  <div class="ks">
    <div class="kp b">
      <div class="kl">Títulos em Aberto</div>
      <div class="kv" id="kpiAbertos">0</div>
      <div class="ksub">projetados para dia útil</div>
    </div>
    <div class="kp g">
      <div class="kl">Títulos Processados</div>
      <div class="kv" id="kpiProcessados">0</div>
      <div class="ksub">remessas enviadas</div>
    </div>
    <div class="kp" id="kpiCardCob">
      <div class="kl">Índice de Cobertura</div>
      <div class="kv" id="kpiCobertura">0%</div>
      <div class="ksub">emitidos / (emitidos + não emitidos)</div>
    </div>
    <div class="kp a" id="kpiCardAjuste">
      <div class="kl">Títulos a Ajustar</div>
      <div class="kv" id="kpiAjustar">{{ qtd_ajustar }}</div>
      <div class="ksub">lançamento = vencimento (até D-1)</div>
    </div>
  </div>
  
  <!-- ── Gráfico por vencimento ── -->
  <div class="c v">
    <div class="ct">Títulos a serem processados - Por Data de Vencimento</div>
    <div class="ct-sub">Agrupado pela data de vencimento (NetDueDate) · verde = já emitido, roxo = ainda não emitido · ciano = cedido a FIDC · laranja = outras formas de pagamento (PIX, cartão, TED…), só volume</div>
    <div class="cw" style="height:230px"><canvas id="cVencimento"></canvas></div>
  </div>
  
  <!-- ── Gráfico abertos ── -->
  <div class="c b">
    <div class="ct">Títulos a serem processados - por Data de Lançamento</div>
    <div class="ct-sub">Regra: Data de faturamento + 1 DU · verde = emitido (mesmo que atrasado), atribuído ao dia original · ciano = cedido a FIDC · laranja = outras formas de pagamento (PIX, cartão, TED…), só volume</div>
    <div class="cw" style="height:230px"><canvas id="cAbertos"></canvas></div>
  </div>

  <!-- ── Histórico de títulos a ajustar ── -->
  <div class="c a">
    <div class="ct">Histórico de Títulos a Ajustar</div>
    <div class="ct-sub">Laranja: snapshot do primeiro acesso de cada dia (lançamento = vencimento, até D-1) · Verde: reparcelados pela automação no dia · Roxo: conciliados com adiantamento/pagamento no dia</div>
    <div class="cw" style="height:200px"><canvas id="cAjusteHistorico"></canvas></div>
  </div>
  
  <!-- ── Gráfico processados ── -->
  <div class="c g">
    <div class="ct">Remessas Geradas por Casa Bancária</div>
    <div class="ct-sub">Volume empilhado por dia e banco emissor</div>
    <div class="cw" style="height:250px"><canvas id="cProcessados"></canvas></div>
  </div>

  <!-- ── Tabela bancos ── -->
  <div class="c">
    <div class="ct">Consolidado por Casa Bancária</div>
    <div style="overflow-x:auto">
      <table class="vt" id="tBancos">
        <thead><tr><th>Banco</th><th style="text-align:center">Títulos</th><th>% da Remessa</th></tr></thead>
        <tbody></tbody>
      </table>
    </div>
  </div>

  <!-- ══════════════════════════════════════════════════════
       SEÇÃO VELOCIDADE
  ══════════════════════════════════════════════════════ -->
  </div><!-- /tab-geracao -->

  <div id="tab-velocidade" class="tab-content">

  <div class="section-sep">⏱ Velocidade de Processamento — Dt_lcto + CreationTime → Remessa</div>

  <!-- KPIs velocidade -->
  <div style="display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin-bottom:16px">
    <div class="kp v">
      <div class="kl">Tempo Médio — Ajustes</div>
      <div class="kv" id="kpiVelMedia">—</div>
      <div class="ksub">da emissão SAP até a remessa</div>
    </div>
    <div class="kp" id="kpiCard4h">
      <div class="kl">Dentro da Meta (48h)</div>
      <div class="kv" id="kpiVel4h">—</div>
      <div class="ksub" id="kpiVel4hSub">dos títulos avaliados</div>
    </div>
    <div class="kp" id="kpiCardPior">
      <div class="kl">Maior Atraso Registrado</div>
      <div class="kv" id="kpiVelPior">—</div>
      <div class="ksub">pior caso no período</div>
    </div>
    <div class="kp a" id="kpiCardComp">
      <div class="kl">Automação vs Referência Anterior</div>
      <div style="display:flex;align-items:baseline;gap:6px;margin-bottom:4px">
        <span style="font-size:11px;color:var(--t2)">Referência anterior:</span>
        <span class="kv" style="font-size:1.4rem" id="kpiHistMedia">—</span>
      </div>
      <div style="display:flex;align-items:baseline;gap:6px">
        <span style="font-size:11px;color:var(--t2)">Automação:</span>
        <span class="kv" style="font-size:1.4rem" id="kpiAtualMedia">—</span>
      </div>
      <div class="ksub" id="kpiCompSub">comparação de médias</div>
    </div>
  </div>

  <!-- Gráfico linha velocidade -->
  <div class="c v">
    <div class="ct">Velocidade Diária — Tempo Médio (horas)</div>
    <div class="ct-sub">
      Roxo = automação (média diária + faixa mín–máx) · Âmbar = histórico manual ·
      Tracejado roxo = média geral automação · Tracejado âmbar = média histórico
    </div>
    <div class="cw" style="height:280px"><canvas id="cVelocidade"></canvas></div>
    <div style="display:flex;gap:20px;margin-top:12px;flex-wrap:wrap">
      <div style="display:flex;align-items:center;gap:6px;font-size:11px;color:var(--t2)">
        <span style="width:18px;height:2.5px;background:#a78bfa;border-radius:2px;display:inline-block"></span>Automação — média diária
      </div>
      <div style="display:flex;align-items:center;gap:6px;font-size:11px;color:var(--t2)">
        <span style="width:18px;height:10px;background:#a78bfa;opacity:.15;border-radius:2px;display:inline-block"></span>Variação mín–máx
      </div>
      <div style="display:flex;align-items:center;gap:6px;font-size:11px;color:var(--t2)">
        <span style="width:18px;height:2.5px;background:#f5a623;border-radius:2px;display:inline-block"></span>Histórico manual
      </div>
      <div style="display:flex;align-items:center;gap:6px;font-size:11px;color:var(--t2)">
        <svg width="18" height="10"><line x1="0" y1="5" x2="18" y2="5" stroke="#a78bfa" stroke-width="1.5" stroke-dasharray="4 2" stroke-opacity=".65"/></svg>Média geral automação
      </div>
      <div style="display:flex;align-items:center;gap:6px;font-size:11px;color:var(--t2)">
        <svg width="18" height="10"><line x1="0" y1="5" x2="18" y2="5" stroke="#f5a623" stroke-width="1.5" stroke-dasharray="4 2" stroke-opacity=".65"/></svg>Média histórico
      </div>
      <div style="display:flex;align-items:center;gap:6px;font-size:11px;color:var(--t2)">
        <svg width="18" height="10"><line x1="0" y1="5" x2="18" y2="5" stroke="#f06060" stroke-width="1.5" stroke-dasharray="4 2" stroke-opacity=".65"/></svg>Meta oficial (48h)
      </div>
    </div>
  </div>

  <!-- Gráfico velocidade de processamento (estimativa por regra de banco) -->
  <div class="c v">
    <div class="ct">Velocidade de Processamento</div>
    <div class="ct-sub">
      Estimativa por banco presente no dia (não é tempo real) ·
      Linha 1: BB/ABC/Santander = 1min por banco · outros = 1h por banco ·
      Linha 2: BB/Santander = 1h por banco · outros = 3min por título + 1,5h por banco
    </div>
    <div class="cw" style="height:280px"><canvas id="cVelProcessamento"></canvas></div>
  </div>

  <!-- Tabela detalhe velocidade -->
  <div class="c v">
    <div class="ct">Detalhe Diário de Velocidade</div>
    <div class="ct-sub">Verde ≤ 24h · Âmbar ≤ 48h (dentro da meta) · Vermelho &gt; 48h (fora da meta)</div>
    <div style="overflow-x:auto">
      <table class="vt" id="tVelocidade">
        <thead>
          <tr>
            <th>Data Processamento</th>
            <th style="text-align:center">Títulos</th>
            <th style="text-align:center">Mais Rápido</th>
            <th style="text-align:center">Média do Dia</th>
            <th style="text-align:center">Mais Lento</th>
            <th style="text-align:center">Classificação</th>
          </tr>
        </thead>
        <tbody></tbody>
        <tfoot>
          <tr>
            <td colspan="2" style="color:var(--t2);font-size:10px">Média geral do período</td>
            <td></td>
            <td style="text-align:center;font-weight:700;font-size:13px" id="tfMediaGeral">—</td>
            <td></td>
            <td style="text-align:center" id="tfBadgeGeral"></td>
          </tr>
        </tfoot>
      </table>
    </div>
  </div>

  </div><!-- /tab-velocidade -->

  <!-- ══════════════════════════════════════════════════════
       ABA EXCLUSÃO DE TÍTULOS (TODOS OS BANCOS)
  ══════════════════════════════════════════════════════ -->
  <div id="tab-exclusao" class="tab-content">

  <style>
  #tab-exclusao .exc-top{display:flex;flex-wrap:wrap;gap:14px;align-items:flex-end;margin-bottom:14px}
  #tab-exclusao input[type="date"]{background:var(--sur);border:1px solid var(--bdr2);color:var(--t);
    font-family:var(--FM);font-size:11px;padding:4px 8px;border-radius:4px;color-scheme:dark}
  #tab-exclusao .exc-sel{display:flex;gap:18px;align-items:center;margin:4px 2px 12px;font-size:11px;color:var(--t2)}
  #tab-exclusao .exc-sel b{color:var(--t);font-family:var(--FH);font-size:14px}
  #tab-exclusao .btn-del{background:var(--R);color:#fff}
  #tab-exclusao .btn-del:disabled{opacity:.35;cursor:not-allowed}
  #tab-exclusao td.num{text-align:right;white-space:nowrap}
  #tab-exclusao tr.sel td{background:rgba(240,96,96,.08)}
  #tab-exclusao .tag{border-radius:4px;padding:1px 7px;font-size:10px;font-weight:600}
  #tab-exclusao .tag-sfr{background:#1e3a5f;color:#9cc9f0}
  #tab-exclusao .tag-sof{background:#3b2a5f;color:#c9b3f5}
  #tab-exclusao .muted{color:var(--t3)}
  #tab-exclusao .exc-aviso{border:1px solid var(--A);color:var(--A);border-radius:5px;padding:8px 12px;
    font-size:11px;margin-bottom:12px;display:none}
  #excModal{position:fixed;inset:0;background:rgba(0,0,0,.65);display:none;align-items:center;
    justify-content:center;z-index:9999}
  #excModal .box{background:var(--card);border:1px solid var(--bdr2);border-radius:8px;padding:22px;
    width:min(460px,92vw)}
  #excModal h3{margin:0 0 6px;font-family:var(--FH);color:var(--t);font-size:16px}
  #excModal p{color:var(--t2);font-size:11.5px;line-height:1.5;margin:6px 0 14px}
  #excModal .field{margin-bottom:12px}
  #excModal input{width:100%;box-sizing:border-box}
  #excModal input[type="password"]{background:var(--sur);border:1px solid var(--bdr2);color:var(--t);
    font-family:var(--FM);font-size:11px;padding:4px 8px;border-radius:4px;outline:none}
  #excModal .erro{color:var(--R);font-size:11px;min-height:16px;margin-bottom:8px}
  #excModal .acoes{display:flex;gap:8px;justify-content:flex-end}
  </style>

  <div class="section-sep">🗑 Títulos criados em todos os bancos — exclusão da base de processados</div>

  <div class="exc-aviso" id="excAviso"></div>

  <div class="c">
    <div class="ct">Títulos processados</div>
    <div class="ct-sub">Excluir da base remove o título do titulos_processados.csv para que ele possa ser
      processado em outro banco. Depois, altere HouseBank e Atribuição no SAP e baixe o boleto no banco de origem,
      se ele foi registrado.</div>

    <div class="exc-top" style="margin-top:12px">
      <div class="field"><label>Banco</label>
        <select id="excBanco" onchange="excRender()">
          <option value="">Todos os bancos</option>
        </select></div>
      <div class="field"><label>Processado de</label><input type="date" id="excDe" onchange="excRender()"></div>
      <div class="field"><label>até</label><input type="date" id="excAte" onchange="excRender()"></div>
      <div class="field grow"><label>Buscar (referência, cliente, documento)</label>
        <input type="text" id="excBusca" oninput="excRender()" placeholder="ex.: 005306420 ou AGRIPEL"></div>
      <button class="btn btn-reset" onclick="excLimparFiltros()">Limpar filtros</button>
      <button class="btn btn-reset" onclick="excCarregar(true)">Atualizar</button>
    </div>

    <div class="exc-sel">
      <span>Exibidos <b id="excQtdVis">0</b></span>
      <span>Selecionados <b id="excQtdSel">0</b></span>
      <span>Valor selecionado <b id="excValSel">R$ 0,00</b></span>
      <span style="flex:1"></span>
      <button class="btn btn-del" id="excBtnExcluir" disabled onclick="excAbrirModal()">Excluir da base</button>
    </div>

    <div style="overflow-x:auto">
      <table class="vt" id="excTabela">
        <thead><tr>
          <th style="width:28px"><input type="checkbox" id="excTodos" onchange="excMarcarTodos(this.checked)"></th>
          <th>Processado em</th><th>Banco</th><th>Empresa</th><th>Documento</th><th>Item</th>
          <th>Referência</th><th>Cliente</th><th style="text-align:right">Valor</th>
          <th>Vencimento</th><th>Situação SAP</th>
        </tr></thead>
        <tbody><tr><td colspan="11" class="muted">Abra a aba para carregar…</td></tr></tbody>
      </table>
    </div>
  </div>

  <div class="c">
    <div class="ct">Últimas exclusões</div>
    <div class="ct-sub">Registro em exclusoes_titulos_log.csv</div>
    <div style="overflow-x:auto">
      <table class="vt" id="excLog">
        <thead><tr><th>Data/hora</th><th>Quem</th><th>Banco</th><th>Título</th>
          <th>Referência</th><th>Cliente</th><th style="text-align:right">Valor</th></tr></thead>
        <tbody><tr><td colspan="7" class="muted">—</td></tr></tbody>
      </table>
    </div>
  </div>

  <div id="excModal">
    <div class="box">
      <h3>Excluir da base</h3>
      <p id="excModalResumo"></p>
      <div class="field"><label>Seu nome</label><input type="text" id="excNome" autocomplete="name"></div>
      <div class="field"><label>Senha</label><input type="password" id="excSenha" autocomplete="off"></div>
      <div class="erro" id="excErro"></div>
      <div class="acoes">
        <button class="btn btn-reset" onclick="excFecharModal()">Cancelar</button>
        <button class="btn btn-del" id="excBtnConfirmar" onclick="excConfirmar()">Confirmar exclusão</button>
      </div>
    </div>
  </div>

  </div><!-- /tab-exclusao -->


</div><!-- /main -->



<script>
Chart.register(ChartDataLabels);

// ── dados injetados pelo Flask ─────────────────────────────
const rawAbertos     = {{ abertos_json | safe }};
const rawVencimento  = {{ vencimento_json | safe }};
const rawProcessados = {{ processados_json | safe }};
const rawHistorico   = {{ historico_json | safe }};
const AJUSTE_HISTORICO = {{ historico_ajuste_json | safe }};

Chart.defaults.color       = '#8aab94';
Chart.defaults.font.family = 'JetBrains Mono';
const GR = { color:'#1d2f20', drawBorder:false };

// ── cores bancos ───────────────────────────────────────────
const BANK_COLORS = {
  "BANCO DO BRASIL": "#FCDB00",
  "ITAU":            "#EC7000",
  "SANTANDER":       "#EC0000",
  "SAFRA":           "#003A70",
  "ABC":             "#00A86B",
  "SOFISA":           "#8AAB94"
};

// ── instâncias de chart ────────────────────────────────────
let chA=null, chP=null, chC=null, chV=null, chVP=null, chAjuste=null, chVenc=null;

// ══════════════════════════════════════════════════════════
// UTILITÁRIOS DE DATA/TEMPO
// ══════════════════════════════════════════════════════════

function normalizarData(data) {
  if (!data) return null;
  data = String(data).trim();
  if (/^\d{4}-\d{2}-\d{2}$/.test(data)) return data;
  if (/^\d{2}\/\d{2}\/\d{4}$/.test(data)) {
    const [d,m,y] = data.split('/');
    return `${y}-${m}-${d}`;
  }
  if (/^\d{2}\.\d{2}\.\d{4}$/.test(data)) {
    const [d,m,y] = data.split('.');
    return `${y}-${m}-${d}`;
  }
  return null;
}

function formatarDataBR(data) {
  const n = normalizarData(data);
  if (!n) return data;
  const [y,m,d] = n.split('-');
  return `${d}/${m}/${y}`;
}

// Formata horas → "2h 30min", "45min", "< 1min"
function fmtHoras(h) {
  if (h === null || h === undefined || isNaN(h)) return '—';
  const totalMin = Math.round(h * 60);
  if (totalMin < 1)  return '< 1min';
  if (totalMin < 60) return `${totalMin}min`;
  const hh = Math.floor(totalMin / 60);
  const mm  = totalMin % 60;
  return mm === 0 ? `${hh}h` : `${hh}h ${mm}min`;
}

// Cor semáforo — Verde ≤ 24h · Âmbar ≤ 48h (meta) · Vermelho > 48h (fora da meta)
const META_HORAS = 48;

function velColor(h) {
  if (h === null || h === undefined || isNaN(h)) return 'var(--t2)';
  if (h <= 24) return '#3ecf6e';
  if (h <= META_HORAS) return '#f5a623';
  return '#f06060';
}
function velBadgeClass(h) {
  if (h === null || h === undefined || isNaN(h)) return 'badge';
  if (h <= 24) return 'badge badge-g';
  if (h <= META_HORAS) return 'badge badge-a';
  return 'badge badge-r';
}
function velKpiClass(h) {
  if (h === null || h === undefined || isNaN(h)) return 'kp';
  if (h <= 24) return 'kp g';
  if (h <= META_HORAS) return 'kp a';
  return 'kp r';
}

// ══════════════════════════════════════════════════════════
// CÁLCULO DE VELOCIDADE
// "Dt_lcto YYYY-MM-DD" + "CreationTime HH:MM:SS"  → início
// "Data_Processamento YYYY-MM-DD" + "Hora_Processamento HH:MM:SS" → fim
// delta em horas (float)
// ══════════════════════════════════════════════════════════

function calcularDeltaHoras(r) {
  const dtLcto   = normalizarData(r.Dt_lcto);
  const ctTime   = r.CreationTime   || '';
  const dtProc   = normalizarData(r.Data_Processamento);
  const horaProc = r.Hora_Processamento || '';

  if (!dtLcto || !ctTime || !dtProc) return null;

  const inicio = new Date(`${dtLcto}T${ctTime}`);
  const fim    = new Date(`${dtProc}T${horaProc || '00:00:00'}`);

  if (isNaN(inicio) || isNaN(fim)) return null;

  const ms = fim - inicio;
  if (ms < 0) return null;

  return parseFloat((ms / 3600000).toFixed(1));
}

function buildVelocidadeData(filtered) {
  const comDelta = filtered
    .map(r => ({ ...r, horas: calcularDeltaHoras(r) }))
    .filter(r => r.horas !== null);

  if (!comDelta.length) return { rows:[], mediaGeral:null, pctMenos4h:0, pior:null, total:0 };

  // Agrupar pela data de processamento
  const dayMap = {};
  comDelta.forEach(r => {
    const d = normalizarData(r.Data_Processamento);
    if (!d) return;
    if (!dayMap[d]) dayMap[d] = { soma:0, count:0, min:Infinity, max:-Infinity };
    dayMap[d].soma  += r.horas;
    dayMap[d].count += 1;
    dayMap[d].min    = Math.min(dayMap[d].min, r.horas);
    dayMap[d].max    = Math.max(dayMap[d].max, r.horas);
  });

  const rows = Object.entries(dayMap)
    .sort(([a],[b]) => a.localeCompare(b))
    .map(([date, v]) => ({
      date,
      media:  parseFloat((v.soma  / v.count).toFixed(1)),
      minimo: parseFloat(v.min.toFixed(1)),
      maximo: parseFloat(v.max.toFixed(1)),
      volume: v.count,
    }));

  const totalHoras   = comDelta.reduce((s,r) => s + r.horas, 0);
  const mediaGeral   = parseFloat((totalHoras / comDelta.length).toFixed(1));
  const dentroMeta    = comDelta.filter(r => r.horas <= META_HORAS).length;  // meta oficial: ≤48h
  const pctDentroMeta = Math.round((dentroMeta / comDelta.length) * 100);
  const pior         = parseFloat(Math.max(...comDelta.map(r => r.horas)).toFixed(1));

  // mediana para linha de referência do gráfico
  const sorted  = [...comDelta].sort((a,b) => a.horas - b.horas);
  const mid     = Math.floor(sorted.length / 2);
  const mediana = sorted.length % 2 !== 0
    ? sorted[mid].horas
    : parseFloat(((sorted[mid-1].horas + sorted[mid].horas) / 2).toFixed(1));

  // média do histórico estático (rawHistorico disponível globalmente)
  const mediaHist = (function(){
    const validos = rawHistorico.filter(r => r.Delta_h !== null && !isNaN(r.Delta_h));
    if (!validos.length) return null;
    return parseFloat((validos.reduce((s,r)=>s+r.Delta_h,0)/validos.length).toFixed(1));
  })();

  return { rows, mediaGeral, mediana, pctDentroMeta, pior, total:comDelta.length, mediaHist };
}

// ══════════════════════════════════════════════════════════
// VELOCIDADE DE PROCESSAMENTO (estimativa por regra de banco)
// Não usa timestamp real — é uma estimativa de esforço por dia,
// baseada em quais bancos tiveram título naquele dia (mapProc[d]),
// independente da quantidade de títulos por banco (exceto onde
// a regra explicitamente diz "por título").
// ══════════════════════════════════════════════════════════

function ehBB(nomeBanco) {
  const n = (nomeBanco || '').toUpperCase();
  return n.includes('BRASIL') || n.startsWith('BB');
}
function ehSantander(nomeBanco) {
  return (nomeBanco || '').toUpperCase().includes('SANTANDER');
}
function ehABC(nomeBanco) {
  return (nomeBanco || '').toUpperCase().includes('ABC');
}

// Linha 1 — BB, ABC ou Santander: 1min por banco (fixo, não por título)
//           Outros bancos: 1h por banco (fixo, não por título)
function calcLinha1Dia(bancosDoDia) {
  let horas = 0;
  Object.keys(bancosDoDia || {}).forEach(banco => {
    if (ehBB(banco) || ehABC(banco) || ehSantander(banco)) {
      horas += 1/60;   // 1 min
    } else {
      horas += 1;       // 1h
    }
  });
  return horas;
}

// Linha 2 — BB ou Santander: 1h por banco (fixo, não por título)
//           Outros bancos (inclusive ABC): 3min por título + 1,5h por banco (fixo)
function calcLinha2Dia(bancosDoDia) {
  let horas = 0;
  Object.entries(bancosDoDia || {}).forEach(([banco, qtd]) => {
    if (ehBB(banco) || ehSantander(banco)) {
      horas += 1;                          // 1h fixa
    } else {
      horas += (3/60) * Number(qtd||0);    // 3min por título
      horas += 1.5;                        // + 1,5h fixa por banco
    }
  });
  return horas;
}

// ══════════════════════════════════════════════════════════
// TOOLTIP STACKED
// ══════════════════════════════════════════════════════════

const stackedTooltip = {
  backgroundColor:'rgba(16,30,20,0.95)',titleColor:'#dff0e4',
  bodyColor:'#8aab94',borderColor:'#253828',borderWidth:1,padding:10,
  callbacks:{
    label(ctx){
      let v=Number(ctx.raw||0), total=0;
      ctx.chart.data.datasets.forEach(ds=>{total+=Number(ds.data[ctx.dataIndex]||0);});
      const pct=total>0?((v/total)*100).toFixed(1):0;
      return ` ${ctx.dataset.label}: ${v} (${pct}%)`;
    }
  }
};

// ══════════════════════════════════════════════════════════
// INICIALIZAÇÃO
// ══════════════════════════════════════════════════════════

function initFilter() {
  let allDates = [
    ...rawAbertos.filter(r => Number(r.Count||0) > 0).map(r => normalizarData(r.Date)),
    ...rawProcessados.map(r => normalizarData(r.Data_Processamento))
  ].filter(Boolean).sort();

  const hoje = new Date();
  const yF = hoje.getFullYear();
  const mF = String(hoje.getMonth()+1).padStart(2,'0');
  const dF = String(hoje.getDate()).padStart(2,'0');
  const dataFimStr = `${yF}-${mF}-${dF}`;
  document.getElementById('dtEnd').value = dataFimStr;

  const dtInicio = new Date(hoje.getFullYear(), hoje.getMonth(), hoje.getDate());
  const diaOriginal = dtInicio.getDate();
  dtInicio.setMonth(dtInicio.getMonth()-1);
  // Corrige o efeito colateral do JS quando o mês anterior tem menos dias
  // (ex.: 31/03 - 1 mês viraria 31/02 → JS empurra pra 02 ou 03/03; aqui
  // ele fica no último dia do mês anterior em vez de "vazar" pro mês seguinte)
  if (dtInicio.getDate() !== diaOriginal) {
    dtInicio.setDate(0);
  }

  const yi = dtInicio.getFullYear();
  const mi = String(dtInicio.getMonth()+1).padStart(2,'0');
  const di = String(dtInicio.getDate()).padStart(2,'0');
  const calc = `${yi}-${mi}-${di}`;

  document.getElementById('dtStart').value =
    (allDates.length > 0 && calc < allDates[0]) ? allDates[0] : calc;

  applyFilter();
}

function resetFilter() {
  document.getElementById('dtStart').value = '';
  document.getElementById('dtEnd').value   = '';
  applyFilter();
}

// ══════════════════════════════════════════════════════════
// FILTRO PRINCIPAL
// ══════════════════════════════════════════════════════════

function applyFilter() {
  const start = document.getElementById('dtStart').value;
  const end   = document.getElementById('dtEnd').value;

  // "Títulos a Vencer" olha 15 dias além do fim do filtro selecionado —
  // senão nunca apareceria vencimento futuro (a data de vencimento é
  // naturalmente posterior à data de criação usada no filtro normal).
  function somarDias(dataStr, dias) {
    if (!dataStr) return dataStr;
    const d = new Date(dataStr + 'T00:00:00');
    d.setDate(d.getDate() + dias);
    return d.toISOString().slice(0, 10);
  }
  const endVencimento = end ? somarDias(end, 15) : '';

  let filAbertos     = rawAbertos.map(r=>({...r, _data:normalizarData(r.Date)}));
  let filVencimento  = rawVencimento.map(r=>({...r, _data:normalizarData(r.Date)}));
  let filProcessados = rawProcessados.map(r=>({...r, _data:normalizarData(r.Data_Processamento)}));

  if (start && end) {
    filAbertos     = filAbertos.filter(r=>r._data && r._data>=start && r._data<=end);
    filVencimento  = filVencimento.filter(r=>r._data && r._data>=start && r._data<=endVencimento);
    filProcessados = filProcessados.filter(r=>r._data && r._data>=start && r._data<=end);
  }

  // ── mapas de agrupamento ─────────────────────────────────
  let mapAbertos={}, mapAbertosProcessado={}, mapVencimento={}, mapVencimentoProcessado={}, mapProc={}, mapBancos={};
  // "Outras formas de pagamento" (PIX, cartão, TED...): mapa separado, usado
  // SÓ no empilhamento dos dois gráficos "Títulos a serem processados" —
  // não entra em mapAbertos, então KPIs, cobertura e conversão não mudam.
  let mapAbertosOutras={}, mapVencimentoOutras={};
  // "Cedido a FIDC": mesma ideia — categoria própria no empilhamento, fora
  // de emitido/não emitido (e portanto fora do índice de cobertura).
  let mapAbertosFidc={}, mapVencimentoFidc={};

  filAbertos.forEach(r=>{
    if (!r._data) return;
    if (Number(r.Count||0) > 0) {
      mapAbertos[r._data]          = (mapAbertos[r._data]||0) + Number(r.Count||0);
      mapAbertosProcessado[r._data]= (mapAbertosProcessado[r._data]||0) + Number(r.Processado||0);
    }
    if (Number(r.Outras||0) > 0)
      mapAbertosOutras[r._data] = (mapAbertosOutras[r._data]||0) + Number(r.Outras||0);
    if (Number(r.Fidc||0) > 0)
      mapAbertosFidc[r._data] = (mapAbertosFidc[r._data]||0) + Number(r.Fidc||0);
  });

  filVencimento.forEach(r=>{
    if (!r._data) return;
    if (Number(r.Count||0) > 0) {
      mapVencimento[r._data]          = (mapVencimento[r._data]||0) + Number(r.Count||0);
      mapVencimentoProcessado[r._data]= (mapVencimentoProcessado[r._data]||0) + Number(r.Processado||0);
    }
    if (Number(r.Outras||0) > 0)
      mapVencimentoOutras[r._data] = (mapVencimentoOutras[r._data]||0) + Number(r.Outras||0);
    if (Number(r.Fidc||0) > 0)
      mapVencimentoFidc[r._data] = (mapVencimentoFidc[r._data]||0) + Number(r.Fidc||0);
  });

  filProcessados.forEach(r=>{
    const d=r._data, b=r.HouseBank||'OUTRO';
    if (!d) return;
    if (!mapProc[d]) mapProc[d]={};
    mapProc[d][b] = (mapProc[d][b]||0)+1;
    mapBancos[b]  = (mapBancos[b]||0)+1;
  });

  const setDatas      = new Set([...Object.keys(mapAbertos),...Object.keys(mapProc)]);
  const arrayDatas    = Array.from(setDatas).sort();
  // "Processado" = títulos daquele dia (mesma chave usada no tempo de
  // criação) que já viraram remessa, mesmo que o processamento em si
  // tenha ocorrido depois (atrasado) — por isso é atribuído aqui, na
  // data original, e não na data em que foi de fato processado.
  // eixo próprio do gráfico de lançamento: as mesmas datas de antes + dias
  // que só têm "outras formas de pagamento" (pra não esconder esse volume)
  const arrayDatasAbertos = Array.from(new Set([...arrayDatas, ...Object.keys(mapAbertosOutras), ...Object.keys(mapAbertosFidc)])).sort();
  const dadosAbertosProcessado = arrayDatasAbertos.map(d=>Math.min(mapAbertosProcessado[d]||0, mapAbertos[d]||0));
  const dadosAbertosPendente   = arrayDatasAbertos.map((d,i)=>Math.max((mapAbertos[d]||0) - dadosAbertosProcessado[i], 0));
  const dadosAbertosOutras     = arrayDatasAbertos.map(d=>mapAbertosOutras[d]||0);
  const dadosAbertosFidc       = arrayDatasAbertos.map(d=>mapAbertosFidc[d]||0);
  const bancosPresentes = Object.keys(mapBancos);

  // ── vencimento (data-alvo própria, eixo x separado do de abertos) ──
  const arrayDatasVenc = Array.from(new Set([...Object.keys(mapVencimento), ...Object.keys(mapVencimentoOutras), ...Object.keys(mapVencimentoFidc)])).sort();
  const dadosVencimentoProcessado = arrayDatasVenc.map(d=>Math.min(mapVencimentoProcessado[d]||0, mapVencimento[d]||0));
  const dadosVencimentoPendente   = arrayDatasVenc.map((d,i)=>Math.max((mapVencimento[d]||0) - dadosVencimentoProcessado[i], 0));
  const dadosVencimentoOutras     = arrayDatasVenc.map(d=>mapVencimentoOutras[d]||0);
  const dadosVencimentoFidc       = arrayDatasVenc.map(d=>mapVencimentoFidc[d]||0);

  const datasetsProc = bancosPresentes.map(banco=>({
    label: banco,
    data:  arrayDatas.map(d=>(mapProc[d]&&mapProc[d][banco])||0),
    backgroundColor: BANK_COLORS[banco]||BANK_COLORS["OUTRO"],
    borderRadius:4,
    datalabels:{color:'#0c1810',font:{weight:'bold',size:10},formatter:v=>v>0?v:''}
  }));

  const dadosPct = arrayDatas.map(d=>{
    const ab = mapAbertos[d]||0;
    const ge = mapProc[d] ? Object.values(mapProc[d]).reduce((a,b)=>a+b,0) : 0;
    if (ab===0) return ge>0 ? 100.0 : 0.0;
    return Number(((ge/ab)*100).toFixed(1));
  });

  // ── KPIs volume ──────────────────────────────────────────
  const totAbertos    = filAbertos.reduce((s,r)=>s+Number(r.Count||0),0);
  const totProcessados= filProcessados.length;

  document.getElementById('kpiAbertos').innerText    = totAbertos.toLocaleString('pt-BR');
  document.getElementById('kpiProcessados').innerText= totProcessados.toLocaleString('pt-BR');

  // Índice de cobertura = emitidos / (emitidos + não emitidos), mesma base
  // do gráfico "Títulos a serem processados" por lançamento no período.
  // FIDC e outras formas de pagamento ficam fora (não são emitidos por nós).
  const totEmitidos    = dadosAbertosProcessado.reduce((a,b)=>a+b,0);
  const totNaoEmitidos = dadosAbertosPendente.reduce((a,b)=>a+b,0);
  const denCob         = totEmitidos + totNaoEmitidos;

  const cCard = document.getElementById('kpiCardCob');
  cCard.className = 'kp';
  if (denCob > 0) {
    const cobPct = ((totEmitidos/denCob)*100).toFixed(1);
    document.getElementById('kpiCobertura').innerText = `${cobPct}%`;
    const numCob = parseFloat(cobPct);
    cCard.classList.add(numCob>=90?'g':numCob>=70?'a':'r');
  } else {
    document.getElementById('kpiCobertura').innerText = '—';
  }

  // ── tabela bancos ─────────────────────────────────────────
  const tb = document.querySelector('#tBancos tbody');
  tb.innerHTML='';
  Object.keys(mapBancos).sort((a,b)=>mapBancos[b]-mapBancos[a]).forEach(banco=>{
    const cnt=mapBancos[banco];
    const pct=totProcessados>0?((cnt/totProcessados)*100).toFixed(1):'0.0';
    const col=BANK_COLORS[banco]||BANK_COLORS["OUTRO"];
    tb.innerHTML+=`
      <tr>
        <td><strong style="color:${col}">● ${banco}</strong></td>
        <td style="text-align:center;font-weight:bold">${cnt}</td>
        <td>
          <div class="bar-bg"><div class="bar-fg" style="width:${pct}%;background:${col}"></div></div>
          <span style="color:var(--t2)">${pct}%</span>
        </td>
      </tr>`;
  });

  // ══════════════════════════════════════════════════════
  // VELOCIDADE
  // ══════════════════════════════════════════════════════

  const vel = buildVelocidadeData(filProcessados);

  // KPIs velocidade
  document.getElementById('kpiVelMedia').innerText = fmtHoras(vel.mediaGeral);
  document.getElementById('kpiVel4h').innerText    = vel.mediaGeral!==null ? `${vel.pctDentroMeta}%` : '—';
  document.getElementById('kpiVel4hSub').innerText = `dos ${vel.total} títulos avaliados`;
  document.getElementById('kpiVelPior').innerText  = fmtHoras(vel.pior);

  // classes semáforo dos KPIs
  document.getElementById('kpiCard4h').className   = vel.pctDentroMeta>=70?'kp g':vel.pctDentroMeta>=40?'kp a':'kp r';
  document.getElementById('kpiCardPior').className = vel.pior!==null ? velKpiClass(vel.pior) : 'kp';

  // ══════════════════════════════════════════════════════
  // VELOCIDADE DE PROCESSAMENTO (estimativa por regra de banco)
  // ══════════════════════════════════════════════════════

  const diasComProcessamento = arrayDatas.filter(d => mapProc[d] && Object.keys(mapProc[d]).length>0);
  const velProcLinha1 = arrayDatas.map(d => mapProc[d] ? calcLinha1Dia(mapProc[d]) : 0);
  const velProcLinha2 = arrayDatas.map(d => mapProc[d] ? calcLinha2Dia(mapProc[d]) : 0);

  const mediaLinha1 = diasComProcessamento.length
    ? diasComProcessamento.reduce((s,d)=>s+calcLinha1Dia(mapProc[d]),0) / diasComProcessamento.length
    : null;
  const mediaLinha2 = diasComProcessamento.length
    ? diasComProcessamento.reduce((s,d)=>s+calcLinha2Dia(mapProc[d]),0) / diasComProcessamento.length
    : null;

  // KPI comparação — Linha 1 (automação) vs Linha 2 (referência anterior)
  document.getElementById('kpiHistMedia').innerText  = fmtHoras(mediaLinha2);
  document.getElementById('kpiAtualMedia').innerText = fmtHoras(mediaLinha1);
  if (mediaLinha1 !== null && mediaLinha2 !== null) {
    const ganho = mediaLinha2 - mediaLinha1;
    const pct   = mediaLinha2 > 0 ? Math.round((ganho / mediaLinha2) * 100) : 0;
    const sinal = ganho >= 0 ? '▼' : '▲';
    const cor   = ganho >= 0 ? '#3ecf6e' : '#f06060';
    document.getElementById('kpiCompSub').innerHTML =
      `<span style='color:${cor};font-weight:bold'>${sinal} ${Math.abs(pct)}% ${ganho>=0?'mais rápido':'mais lento'} que a referência anterior</span>`;
  }

  if (chVP) chVP.destroy();
  chVP = new Chart(document.getElementById('cVelProcessamento'), {
    type:'line',
    data:{
      labels: arrayDatas.map(formatarDataBR),
      datasets:[
        {
          label:'Linha 1',
          data: velProcLinha1,
          borderColor:'#3ecf6e',
          backgroundColor:'rgba(62,207,110,0)',
          borderWidth:2.5,
          tension:0.4,
          pointRadius:4,
          pointBackgroundColor:'#3ecf6e',
          pointBorderColor:'#0c1810',
          pointBorderWidth:1,
          datalabels:{color:'#3ecf6e',anchor:'end',align:'top',font:{size:10,weight:'bold'},formatter:v=>fmtHoras(v)},
        },
        {
          label:'Linha 2',
          data: velProcLinha2,
          borderColor:'#f5a623',
          backgroundColor:'rgba(245,166,35,0)',
          borderWidth:2.5,
          tension:0.4,
          pointRadius:4,
          pointBackgroundColor:'#f5a623',
          pointBorderColor:'#0c1810',
          pointBorderWidth:1,
          datalabels:{color:'#f5a623',anchor:'end',align:'top',font:{size:10,weight:'bold'},formatter:v=>fmtHoras(v)},
        },
      ]
    },
    options:{
      responsive:true,maintainAspectRatio:false,
      interaction:{mode:'index',intersect:false},
      plugins:{
        legend:{position:'bottom',labels:{color:'#8aab94',usePointStyle:true,padding:20}},
        tooltip:{
          backgroundColor:'rgba(16,30,20,0.95)',titleColor:'#dff0e4',bodyColor:'#8aab94',
          borderColor:'#253828',borderWidth:1,padding:10,
          callbacks:{label:c=>` ${c.dataset.label}: ${fmtHoras(c.raw)}`}
        },
      },
      scales:{
        x:{grid:{display:false}},
        y:{
          beginAtZero:true,grid:GR,
          ticks:{callback:v=>v===0?'0':v<1?`${Math.round(v*60)}min`:`${v}h`,color:'#8aab94',font:{size:11}}
        }
      }
    }
  });

  // ── Gráfico de linha velocidade ────────────────────────
  const velLabels = vel.rows.map(r=>formatarDataBR(r.date));
  const velMedia  = vel.rows.map(r=>r.media);
  const velMin    = vel.rows.map(r=>r.minimo);
  const velMax    = vel.rows.map(r=>r.maximo);
  const velVol    = vel.rows.map(r=>r.volume);

  // ── dados histórico para o gráfico ─────────────────────────────────
  // rawHistorico: [{Data_Processamento: "YYYY-MM-DD", Delta_h: float}]
  // Agrupa por data (média), alinha no mesmo eixo X da automação
  const histMap = {};
  rawHistorico.forEach(r => {
    const d = normalizarData(r.Data_Processamento);
    if (!d) return;
    if (start && end && (d < start || d > end)) return;  // ← filtro de data
    if (!histMap[d]) histMap[d] = { soma:0, count:0 };
    histMap[d].soma  += r.Delta_h;
    histMap[d].count += 1;
  });

  // labels unificados: automação + histórico, ordenados
  const allVelDates = [...new Set([...velLabels.map((_,i)=>vel.rows[i]?.date), ...Object.keys(histMap)])].filter(Boolean).sort();
  const velLabelsUnif = allVelDates.map(formatarDataBR);

  // re-mapeia automação para o eixo unificado
  const velMediaUnif = allVelDates.map(d => {
    const r = vel.rows.find(x => x.date === d);
    return r ? r.media  : null;
  });
  const velMaxUnif = allVelDates.map(d => {
    const r = vel.rows.find(x => x.date === d);
    return r ? r.maximo : null;
  });
  const velMinUnif = allVelDates.map(d => {
    const r = vel.rows.find(x => x.date === d);
    return r ? r.minimo : null;
  });
  const velVolUnif = allVelDates.map(d => {
    const r = vel.rows.find(x => x.date === d);
    return r ? r.volume : 0;
  });

  // histórico no eixo unificado
  const histMedia = allVelDates.map(d => {
    const v = histMap[d];
    return v ? parseFloat((v.soma / v.count).toFixed(1)) : null;
  });

  // média global histórico (para linha de ref)
  const histValidos = rawHistorico.filter(r => {
    const d = normalizarData(r.Data_Processamento);
    return d && (!start || !end || (d >= start && d <= end));
  });
  const mediaHistRef = histValidos.length
    ? parseFloat((histValidos.reduce((s,r) => s + r.Delta_h, 0) / histValidos.length).toFixed(1))
    : null;

  if (chV) chV.destroy();
  chV = new Chart(document.getElementById('cVelocidade'), {
    type:'line',
    data:{
      labels: velLabelsUnif,
      datasets:[
        // faixa máx (área superior) — automação
        {
          label:'Máximo',
          data: velMaxUnif,
          borderColor:'rgba(167,139,250,0.35)',
          borderWidth:1,
          borderDash:[3,2],
          backgroundColor:'rgba(167,139,250,0.12)',
          fill:true,
          pointRadius:0,
          tension:0.4,
          datalabels:{display:false},
          order:3,
        },
        // faixa mín (cobre a área inferior com a cor de fundo)
        {
          label:'Mínimo',
          data: velMinUnif,
          borderColor:'rgba(167,139,250,0.35)',
          borderWidth:1,
          borderDash:[3,2],
          backgroundColor:'#141f17',   // mesma cor do card
          fill:true,
          pointRadius:0,
          tension:0.4,
          datalabels:{display:false},
          order:2,
        },
        // linha principal: média
        {
          label:'Média',
          data: velMediaUnif,
          borderColor:'#a78bfa',
          borderWidth:2.5,
          backgroundColor:'rgba(167,139,250,0)',
          fill:false,
          tension:0.4,
          pointRadius:4,
          pointBackgroundColor:'#a78bfa',
          pointBorderColor:'#0c1810',
          pointBorderWidth:1,
          datalabels:{
            color:'#a78bfa',
            anchor:'end',
            align:'top',
            font:{size:10,weight:'bold'},
            formatter:v=>fmtHoras(v),
          },
          order:1,
          // volume disponível no payload via custom property
          _volumes: velVolUnif,
        },
        // série histórico manual — cor âmbar
        {
          label:'Histórico',
          data: histMedia,
          borderColor:'#f5a623',
          borderWidth:2,
          backgroundColor:'rgba(245,166,35,0)',
          fill:false,
          tension:0.4,
          pointRadius:4,
          pointBackgroundColor:'#f5a623',
          pointBorderColor:'#0c1810',
          pointBorderWidth:1,
          spanGaps:false,
          datalabels:{
            color:'#f5a623',
            anchor:'end',
            align:'top',
            font:{size:10,weight:'bold'},
            formatter:v=>v!==null?fmtHoras(v):'',
          },
          order:4,
        },
      ]
    },
    options:{
      responsive:true,
      maintainAspectRatio:false,
      interaction:{mode:'index',intersect:false},
      plugins:{
        legend:{display:false},
        tooltip:{
          backgroundColor:'rgba(16,30,20,0.95)',
          titleColor:'#dff0e4',
          bodyColor:'#8aab94',
          borderColor:'#253828',
          borderWidth:1,
          padding:10,
          callbacks:{
            title(items){
              const idx=items[0].dataIndex;
              const vol=velVolUnif[idx]??'';
              return `${items[0].label}${vol ? '  ('+vol+' títulos automação)' : ''}`;
            },
            label(ctx){
              const name=ctx.dataset.label;
              if (!['Média','Mínimo','Máximo','Histórico'].includes(name)) return null;
              if (ctx.raw === null) return null;
              return ` ${name}: ${fmtHoras(ctx.raw)}`;
            }
          }
        },
        // linha de referência via annotation não disponível aqui;
        // usamos dataset extra como linha horizontal
        datalabels:{},
      },
      scales:{
        x:{grid:{display:false}},
        y:{
          beginAtZero:true,
          grid:GR,
          ticks:{
            callback:v=>v===0?'0':v<1?`${Math.round(v*60)}min`:`${v}h`,
            color:'#8aab94',
            font:{size:11},
          }
        }
      }
    }
  });

  // Linhas de referência (média automação, média histórico e meta oficial 48h) via plugin inline
  // Desenhamos no afterDraw
  chV.options.plugins.afterDraw = undefined; // limpa
  const mediaGeralRef = vel.mediaGeral;
  const medianaRef    = vel.mediana;

  const drawRefLines = {
    id:'refLines',
    afterDraw(chart){
      const {ctx, chartArea:{left,right,top,bottom}, scales:{y}} = chart;
      function drawLine(valor, color, label){
        if (valor===null || valor===undefined || isNaN(valor)) return;
        const yPx = y.getPixelForValue(valor);
        if (yPx < top || yPx > bottom) return; // fora da área visível
        ctx.save();
        ctx.setLineDash([5,3]);
        ctx.strokeStyle=color;
        ctx.lineWidth=1.2;
        ctx.globalAlpha=0.6;
        ctx.beginPath();
        ctx.moveTo(left, yPx);
        ctx.lineTo(right, yPx);
        ctx.stroke();
        ctx.setLineDash([]);
        ctx.globalAlpha=1;
        ctx.fillStyle=color;
        ctx.font='bold 10px JetBrains Mono';
        ctx.textAlign='right';
        ctx.fillText(label, right-4, yPx-4);
        ctx.restore();
      }
      drawLine(mediaGeralRef, '#a78bfa', `Média automação: ${fmtHoras(mediaGeralRef)}`);
      drawLine(mediaHistRef,  '#f5a623', `Média histórico: ${fmtHoras(mediaHistRef)}`);
      drawLine(META_HORAS,    '#f06060', `Meta: ${META_HORAS}h`);
    }
  };

  // Registra o plugin somente neste chart
  chV.config.plugins = [drawRefLines];
  chV.update();

  // ── Tabela velocidade ─────────────────────────────────────
  const tbv  = document.querySelector('#tVelocidade tbody');
  tbv.innerHTML='';
  vel.rows.forEach(r=>{
    const mc = velColor(r.media);
    tbv.innerHTML+=`
      <tr>
        <td style="font-weight:500">${formatarDataBR(r.date)}</td>
        <td style="text-align:center;color:var(--t2)">${r.volume}</td>
        <td style="text-align:center;color:#3ecf6e;font-weight:500">${fmtHoras(r.minimo)}</td>
        <td style="text-align:center;font-weight:700;color:${mc}">${fmtHoras(r.media)}</td>
        <td style="text-align:center;color:#f06060">${fmtHoras(r.maximo)}</td>
        <td style="text-align:center"><span class="${velBadgeClass(r.media)}">${fmtHoras(r.media)}</span></td>
      </tr>`;
  });

  // Rodapé da tabela
  document.getElementById('tfMediaGeral').innerText  = fmtHoras(vel.mediaGeral);
  document.getElementById('tfMediaGeral').style.color= velColor(vel.mediaGeral)||'var(--t)';
  document.getElementById('tfBadgeGeral').innerHTML  =
    vel.mediaGeral!==null
      ? `<span class="${velBadgeClass(vel.mediaGeral)}">${fmtHoras(vel.mediaGeral)}</span>`
      : '—';

  // ══════════════════════════════════════════════════════
  // GRÁFICOS DE VOLUME
  // ══════════════════════════════════════════════════════

  if (chA) chA.destroy();
  chA = new Chart(document.getElementById('cAbertos'),{
    type:'bar',
    data:{
      labels:arrayDatasAbertos.map(formatarDataBR),
      datasets:[
        {
          label:'Outras formas de pagamento',
          data:dadosAbertosOutras,
          backgroundColor:'#f5a623',
          borderColor:'#f5a623',
          borderWidth:1,
          borderRadius:4,
          datalabels:{color:'#0c1810',font:{weight:'bold',size:10},formatter:v=>v>0?v:''}
        },
        {
          label:'Cedido a FIDC',
          data:dadosAbertosFidc,
          backgroundColor:'#22d3ee',
          borderColor:'#22d3ee',
          borderWidth:1,
          borderRadius:4,
          datalabels:{color:'#0c1810',font:{weight:'bold',size:10},formatter:v=>v>0?v:''}
        },
        {
          label:'Emitido',
          data:dadosAbertosProcessado,
          backgroundColor:'#3ecf6e',
          borderColor:'#3ecf6e',
          borderWidth:1,
          borderRadius:4,
          datalabels:{color:'#0c1810',font:{weight:'bold',size:10},formatter:v=>v>0?v:''}
        },
        {
          label:'Não emitido',
          data:dadosAbertosPendente,
          backgroundColor:'rgba(91,164,207,0.2)',
          borderColor:'#5ba4cf',
          borderWidth:1,
          borderRadius:4,
          datalabels:{color:'#dff0e4',font:{size:10},formatter:v=>v>0?v:''}
        }
      ]
    },
    options:{
      responsive:true,maintainAspectRatio:false,
      plugins:{legend:{position:'bottom',labels:{color:'#8aab94',usePointStyle:true,padding:20}},tooltip:{backgroundColor:'rgba(16,30,20,0.95)',titleColor:'#dff0e4',bodyColor:'#5ba4cf'}},
      scales:{x:{stacked:true,grid:{display:false}},y:{stacked:true,beginAtZero:true,grid:GR}}
    }
  });

  if (chVenc) chVenc.destroy();
  chVenc = new Chart(document.getElementById('cVencimento'),{
    type:'bar',
    data:{
      labels:arrayDatasVenc.map(formatarDataBR),
      datasets:[
        {
          label:'Outras formas de pagamento',
          data:dadosVencimentoOutras,
          backgroundColor:'#f5a623',
          borderColor:'#f5a623',
          borderWidth:1,
          borderRadius:4,
          datalabels:{color:'#0c1810',font:{weight:'bold',size:10},formatter:v=>v>0?v:''}
        },
        {
          label:'Cedido a FIDC',
          data:dadosVencimentoFidc,
          backgroundColor:'#22d3ee',
          borderColor:'#22d3ee',
          borderWidth:1,
          borderRadius:4,
          datalabels:{color:'#0c1810',font:{weight:'bold',size:10},formatter:v=>v>0?v:''}
        },
        {
          label:'Emitido',
          data:dadosVencimentoProcessado,
          backgroundColor:'#3ecf6e',
          borderColor:'#3ecf6e',
          borderWidth:1,
          borderRadius:4,
          datalabels:{color:'#0c1810',font:{weight:'bold',size:10},formatter:v=>v>0?v:''}
        },
        {
          label:'Não emitido',
          data:dadosVencimentoPendente,
          backgroundColor:'rgba(167,139,250,0.2)',
          borderColor:'#a78bfa',
          borderWidth:1,
          borderRadius:4,
          datalabels:{color:'#dff0e4',font:{size:10},formatter:v=>v>0?v:''}
        }
      ]
    },
    options:{
      responsive:true,maintainAspectRatio:false,
      plugins:{legend:{position:'bottom',labels:{color:'#8aab94',usePointStyle:true,padding:20}},tooltip:{backgroundColor:'rgba(16,30,20,0.95)',titleColor:'#dff0e4',bodyColor:'#a78bfa'}},
      scales:{x:{stacked:true,grid:{display:false}},y:{stacked:true,beginAtZero:true,grid:GR}}
    }
  });

  if (chP) chP.destroy();
  chP = new Chart(document.getElementById('cProcessados'),{
    type:'bar',
    data:{labels:arrayDatas.map(formatarDataBR),datasets:datasetsProc},
    options:{
      responsive:true,maintainAspectRatio:false,
      plugins:{legend:{position:'bottom',labels:{color:'#8aab94',usePointStyle:true,padding:20}},tooltip:stackedTooltip},
      scales:{x:{stacked:true,grid:{display:false}},y:{stacked:true,beginAtZero:true,grid:GR}}
    }
  });

  if (chC) chC.destroy();
  chC = new Chart(document.getElementById('cPercentual'),{
    type:'line',
    data:{
      labels:arrayDatas.map(formatarDataBR),
      datasets:[{
        label:'% Convertido',
        data:dadosPct,
        borderColor:'#3ecf6e',
        backgroundColor:'rgba(62,207,110,0.1)',
        fill:true,tension:0.4,pointRadius:4,pointBackgroundColor:'#3ecf6e',
        datalabels:{color:'#3ecf6e',anchor:'end',align:'top',formatter:v=>v+'%',font:{size:10,weight:'bold'}}
      }]
    },
    options:{
      responsive:true,maintainAspectRatio:false,
      plugins:{legend:{display:false},tooltip:{backgroundColor:'rgba(16,30,20,0.95)',titleColor:'#dff0e4',bodyColor:'#3ecf6e',callbacks:{label:c=>c.raw+'%'}}},
      scales:{x:{grid:{display:false}},y:{beginAtZero:true,max:120,grid:GR}}
    }
  });
}

// ══════════════════════════════════════════════════════════
// ABAS
// ══════════════════════════════════════════════════════════

function showTab(nome) {
  document.querySelectorAll('.tab-content').forEach(el => el.classList.remove('active'));
  document.querySelectorAll('.tab-btn').forEach(el => el.classList.remove('active'));
  document.getElementById(`tab-${nome}`).classList.add('active');
  document.getElementById(`tabBtn-${nome}`).classList.add('active');
  // Chart.js não calcula largura/altura corretas dentro de um
  // container que estava display:none — força recálculo ao trocar de aba
  window.dispatchEvent(new Event('resize'));
}

// ══════════════════════════════════════════════════════════
// HISTÓRICO DE TÍTULOS A AJUSTAR
// ══════════════════════════════════════════════════════════

function renderAjusteHistorico() {
  const canvas = document.getElementById('cAjusteHistorico');
  if (!canvas) return;
  if (chAjuste) chAjuste.destroy();
  chAjuste = new Chart(canvas, {
    type: 'line',
    data: {
      labels: AJUSTE_HISTORICO.map(r => formatarDataBR(r.data)),
      datasets: [{
        label: 'Títulos a ajustar',
        data: AJUSTE_HISTORICO.map(r => r.quantidade),
        borderColor: '#f5a623',
        backgroundColor: 'rgba(245,166,35,0.12)',
        fill: true, tension: 0.35, pointRadius: 4, pointBackgroundColor: '#f5a623',
        spanGaps: true,
        datalabels: { color: '#f5a623', anchor: 'end', align: 'top', font: { size: 10, weight: 'bold' },
                      display: ctx => ctx.dataset.data[ctx.dataIndex] != null }
      }, {
        label: 'Ajustados pela automação',
        data: AJUSTE_HISTORICO.map(r => r.ajustados),
        borderColor: '#3ddc84',
        backgroundColor: 'rgba(61,220,132,0.10)',
        fill: false, tension: 0.35, pointRadius: 4, pointBackgroundColor: '#3ddc84',
        spanGaps: true,
        datalabels: { color: '#3ddc84', anchor: 'start', align: 'bottom', font: { size: 10, weight: 'bold' },
                      display: ctx => ctx.dataset.data[ctx.dataIndex] != null }
      }, {
        label: 'Conciliados com adiantamento/pagamento',
        data: AJUSTE_HISTORICO.map(r => r.conciliados),
        borderColor: '#a78bfa',
        backgroundColor: 'rgba(167,139,250,0.10)',
        fill: false, tension: 0.35, pointRadius: 4, pointBackgroundColor: '#a78bfa',
        spanGaps: true,
        datalabels: { color: '#a78bfa', anchor: 'start', align: 'bottom', font: { size: 10, weight: 'bold' },
                      display: ctx => ctx.dataset.data[ctx.dataIndex] != null }
      }]
    },
    options: {
      responsive: true, maintainAspectRatio: false,
      plugins: {
        legend: { display: true, position: 'top', align: 'end',
                  labels: { color: '#dff0e4', boxWidth: 10, boxHeight: 10, font: { size: 10 } } },
        tooltip: { backgroundColor: 'rgba(16,30,20,0.95)', titleColor: '#dff0e4', bodyColor: '#dff0e4',
                   mode: 'index', intersect: false }
      },
      scales: { x: { grid: { display: false } }, y: { beginAtZero: true, grid: GR, ticks: { precision: 0 } } }
    }
  });
}

document.addEventListener('DOMContentLoaded', initFilter);
document.addEventListener('DOMContentLoaded', renderAjusteHistorico);
</script>

<script>
(function(){
"use strict";

var BANKS = {{ banks | tojson }};
var ROUTABLE_BANKS = {{ routable_banks | tojson }};
var BANK_ACCENT = {"Safra":"g","ABC":"b","Sofisa":"v","Banco do Brasil":"a","Santander":"r"};
var DEFAULT_RULES = {{ bank_rules_json | safe }};
var SALDO_RAW = {{ saldo_json | safe }};
var ITENS_ALL = {{ itens_json | safe }};
var VENC7_POR_BANCO = {{ venc7_json | safe }};
var VENC15_POR_BANCO = {{ venc15_json | safe }};
var VENC30_POR_BANCO = {{ venc30_json | safe }};
var QTD_TITULOS_POR_BANCO = {{ qtd_titulos_json | safe }};
var VALOR_POR_FIDC = {{ valor_fidc_json | safe }};
var QTD_TITULOS_POR_FIDC = {{ qtd_fidc_json | safe }};
var CARTEIRA_SIMPLES_ABC = { valor: {{ valor_carteira_simples_abc }}, qtd: {{ qtd_carteira_simples_abc }} };
var FIDC_LIST = {{ fidc_list_json | safe }};
var AGING = {{ aging_json | safe }};
var VENCIDO_POR_BANCO = {{ vencido_json | safe }};

var state = {
  rules: DEFAULT_RULES,
  expanded: {}
};
state.itemsToDecide = ITENS_ALL.filter(function(r){ return !r.preAssignedBank; });
state.itemsPreAssigned = ITENS_ALL.filter(function(r){ return r.preAssignedBank; });

function fmtMoney(n){ return "R$ " + n.toLocaleString('pt-BR',{minimumFractionDigits:2,maximumFractionDigits:2}); }
function fmtPct(n){ return (n*100).toLocaleString('pt-BR',{maximumFractionDigits:1}) + "%"; }
function fmtDateBR(iso){ if(!iso) return "—"; var p=iso.split('-'); return p[2]+"/"+p[1]+"/"+p[0]; }
function daysBetween(a,b){ if(!a||!b) return null; return Math.round((new Date(b)-new Date(a))/86400000); }
function esc(s){ return String(s).replace(/[&<>"']/g, function(c){ return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]; }); }

function saldoByCustomerBank(){
  var map = {};
  SALDO_RAW.forEach(function(r){ var k=r.customer+'|'+r.bank; map[k]=(map[k]||0)+r.valor; });
  return map;
}
function totalByBank(){
  // total bruto da carteira (consulta 1, 1 linha por título — bate com o SAP)
  // menos o vencido apurado à parte (consulta 1b); nunca soma o vencido, só abate
  var totals = {}; BANKS.forEach(function(b){ totals[b]=0; });
  SALDO_RAW.forEach(function(r){ totals[r.bank]=(totals[r.bank]||0)+r.valor; });
  BANKS.forEach(function(b){ totals[b] = Math.max(0, totals[b] - (VENCIDO_POR_BANCO[b]||0)); });
  return totals;
}
function preAssignedSummary(){
  var s = {}; BANKS.forEach(function(b){ s[b]={count:0, valor:0}; });
  state.itemsPreAssigned.forEach(function(r){ s[r.preAssignedBank].count++; s[r.preAssignedBank].valor+=r.amount; });
  return s;
}

function renderOverview(){
  var totals = totalByBank();
  var preSum = preAssignedSummary();
  // ordem fixa: BB e Santander na 1ª linha (alinhados com Safra/ABC),
  // 3ª coluna da 1ª linha fica vazia (alinhada sob o Sofisa) — "null" é o espaço vazio
  var CARD_ORDER = ["Banco do Brasil", "Santander", null, "Safra", "ABC", "Sofisa"];
  var html = '';
  CARD_ORDER.forEach(function(bank){
    if(bank === null){
      html += '<div class="spacer"></div>';
      return;
    }
    var r = state.rules[bank];
    var used = totals[bank]||0;
    var accent = BANK_ACCENT[bank] || 'g';
    var used7 = VENC7_POR_BANCO[bank] || 0;
    var used15 = VENC15_POR_BANCO[bank] || 0;
    var used30 = VENC30_POR_BANCO[bank] || 0;
    var qtdTitulos = QTD_TITULOS_POR_BANCO[bank] || 0;
    var cardHtml = '<div class="kp '+accent+'">';
    cardHtml += '<div class="kl"><span>'+bank+'</span><span>'+qtdTitulos+' título(s)</span></div>';
    if(r.limite_carteira!=null){
      // limite_carteira aqui é o piso que a carteira PRECISA ter, não um teto:
      // abaixo dele é vermelho, até 2% acima é amarelo, além disso é verde.
      // (a regra de concentração por sacado continua usando esse valor como teto normalmente)
      var pct = r.limite_carteira>0 ? used/r.limite_carteira : 0;
      var status = pct<1 ? 'under' : (pct<=1.05 ? 'near' : 'over');
      var diff = used - r.limite_carteira;
      var vencPctDaBarra = used>0 ? Math.min(used30/used,1)*100 : 0;
      cardHtml += '<div class="kv">'+fmtPct(pct)+'</div>';
      cardHtml += '<div class="ksub">'+fmtMoney(used)+' usado de '+fmtMoney(r.limite_carteira)+' (mínimo da carteira)</div>';
      cardHtml += '<div class="bar-bg" style="margin-top:12px"><div class="bar-fg '+status+'" style="width:'+Math.min(pct*100,100)+'%">'+
        (used30>0 ? '<div class="bar-fg-venc" style="width:'+vencPctDaBarra+'%"></div>' : '')+
        '</div></div>';
      cardHtml += '<div class="ksub" style="text-align:right;margin-top:4px">'+(diff>=0 ? fmtMoney(diff)+' acima do mínimo' : fmtMoney(-diff)+' abaixo do mínimo')+'</div>';
    } else {
      cardHtml += '<div class="kv">'+fmtMoney(used)+'</div>';
      cardHtml += '<div class="ksub">limite da carteira não informado</div>';
    }
    if(used30>0){
      cardHtml += '<div class="refnote">'+
        '<div style="display:flex;justify-content:space-between"><span>Vencendo em 7 dias</span><b style="color:var(--R)">'+fmtMoney(used7)+'</b></div>'+
        '<div style="display:flex;justify-content:space-between"><span>Vencendo em 15 dias</span><b>'+fmtMoney(used15)+'</b></div>'+
        '<div style="display:flex;justify-content:space-between"><span>Vencendo em 30 dias</span><b>'+fmtMoney(used30)+'</b></div>'+
      '</div>';
    }
    var ps = preSum[bank];
    if(ps.count>0){
      cardHtml += '<div class="refnote"><b>'+ps.count+'</b> título(s) já direcionados via referência — '+fmtMoney(ps.valor)+' já somados acima</div>';
    }
    if(bank === 'ABC' && CARTEIRA_SIMPLES_ABC.qtd > 0){
      cardHtml += '<div class="refnote"><b>'+CARTEIRA_SIMPLES_ABC.qtd+'</b> título(s) na carteira simples — '+
        fmtMoney(CARTEIRA_SIMPLES_ABC.valor)+'</div>';
    }
    cardHtml += '</div>';
    html += cardHtml;
  });
  document.getElementById('overviewCards').innerHTML = html;
}

// ---- gráfico de aging da carteira (vencimento em faixas de 30 dias) ----

function bankHex(bank){
  var cssVar = {"Safra":"--G","ABC":"--B","Sofisa":"--V","Banco do Brasil":"--A","Santander":"--R"}[bank] || "--G";
  return getComputedStyle(document.documentElement).getPropertyValue(cssVar).trim();
}
function fmtMoneyCompact(n){
  var abs = Math.abs(n);
  if(abs>=1e6) return "R$ "+(n/1e6).toLocaleString('pt-BR',{maximumFractionDigits:1})+"M";
  if(abs>=1e3) return "R$ "+(n/1e3).toLocaleString('pt-BR',{maximumFractionDigits:0})+"K";
  return "R$ "+n.toLocaleString('pt-BR',{maximumFractionDigits:0});
}
function niceCeil(value){
  if(value<=0) return 10;
  var exp = Math.floor(Math.log10(value));
  var frac = value/Math.pow(10,exp);
  var niceFrac = frac<=1 ? 1 : (frac<=2 ? 2 : (frac<=5 ? 5 : 10));
  return niceFrac*Math.pow(10,exp);
}
function getAgingTip(){
  var el = document.getElementById('agingTip');
  if(!el){ el=document.createElement('div'); el.id='agingTip'; el.className='aging-tip'; el.style.display='none'; document.body.appendChild(el); }
  return el;
}

function renderAgingLegend(){
  var holder = document.getElementById('agingLegend');
  holder.innerHTML = BANKS.map(function(b){
    return '<span class="legend-item"><span class="legend-swatch" style="background:'+bankHex(b)+'"></span>'+b+'</span>';
  }).join('');
}

function renderAgingSvg(){
  var wrap = document.getElementById('agingChartWrap');
  var buckets = AGING.buckets, porBanco = AGING.porBanco;

  var maxVal = 0;
  buckets.forEach(function(bucket){ BANKS.forEach(function(b){ maxVal = Math.max(maxVal, porBanco[bucket][b]||0); }); });
  if(maxVal<=0){ wrap.innerHTML = '<div class="empty">Nenhum título com vencimento informado na carteira.</div>'; return; }
  var niceMax = niceCeil(maxVal);

  var W=760, H=280, mL=58, mR=10, mT=10, mB=30;
  var plotW = W-mL-mR, plotH = H-mT-mB;
  var bandW = plotW/buckets.length;
  var barThick = Math.min(24, (bandW*0.8 - (BANKS.length-1)*2)/BANKS.length);
  var groupW = barThick*BANKS.length + 2*(BANKS.length-1);

  function y(v){ return plotH*(v/niceMax); }

  var svg = '<svg class="aging-svg" viewBox="0 0 '+W+' '+H+'" preserveAspectRatio="xMidYMid meet">';

  // gridlines + eixo Y
  for(var g=0; g<=4; g++){
    var gv = niceMax*g/4;
    var gy = mT+plotH-y(gv);
    svg += '<line class="aging-grid" x1="'+mL+'" x2="'+(W-mR)+'" y1="'+gy+'" y2="'+gy+'"></line>';
    svg += '<text class="aging-axis-label" x="'+(mL-8)+'" y="'+(gy+3)+'" text-anchor="end">'+fmtMoneyCompact(gv)+'</text>';
  }

  // barras + rótulos do eixo X
  buckets.forEach(function(bucket, i){
    var bandX = mL + i*bandW;
    var groupX = bandX + bandW/2 - groupW/2;
    BANKS.forEach(function(bank, j){
      var val = porBanco[bucket][bank] || 0;
      var barH = Math.max(y(val),0);
      var barX = groupX + j*(barThick+2);
      var barY = mT+plotH-barH;
      var color = bankHex(bank);
      if(barH>0){
        if(barH>4){
          svg += '<rect class="aging-bar" data-bank="'+bank+'" data-bucket="'+esc(bucket)+'" data-valor="'+val+'" '+
            'x="'+barX+'" y="'+barY+'" width="'+barThick+'" height="'+barH+'" rx="4" ry="4" fill="'+color+'"></rect>';
          svg += '<rect x="'+barX+'" y="'+(barY+barH-4)+'" width="'+barThick+'" height="4" fill="'+color+'" pointer-events="none"></rect>';
        } else {
          svg += '<rect class="aging-bar" data-bank="'+bank+'" data-bucket="'+esc(bucket)+'" data-valor="'+val+'" '+
            'x="'+barX+'" y="'+barY+'" width="'+barThick+'" height="'+barH+'" fill="'+color+'"></rect>';
        }
      }
    });
    svg += '<text class="aging-axis-label" x="'+(bandX+bandW/2)+'" y="'+(mT+plotH+18)+'" text-anchor="middle">'+esc(bucket)+'</text>';
  });

  svg += '</svg>';
  wrap.innerHTML = svg;

  var tip = getAgingTip();
  wrap.querySelectorAll('.aging-bar').forEach(function(rect){
    rect.addEventListener('pointermove', function(e){
      var bank = rect.getAttribute('data-bank'), bucket = rect.getAttribute('data-bucket'), valor = parseFloat(rect.getAttribute('data-valor'));
      tip.innerHTML = '<span class="lbl">'+esc(bank)+' · '+esc(bucket)+' dias</span><br><span class="val">'+fmtMoney(valor)+'</span>';
      tip.style.left = (e.clientX+14)+'px';
      tip.style.top = (e.clientY+14)+'px';
      tip.style.display = 'block';
    });
    rect.addEventListener('pointerleave', function(){ tip.style.display='none'; });
  });
}

function renderAgingTable(){
  var wrap = document.getElementById('agingTableWrap');
  var buckets = AGING.buckets, porBanco = AGING.porBanco;
  var totalPorBanco = {}; BANKS.forEach(function(b){ totalPorBanco[b]=0; });
  var html = '<table class="aging"><thead><tr><th>Faixa</th>'+BANKS.map(function(b){return '<th>'+esc(b)+'</th>';}).join('')+'<th>Total</th></tr></thead><tbody>';
  buckets.forEach(function(bucket){
    var linhaTotal = 0;
    html += '<tr><td>'+esc(bucket)+'</td>';
    BANKS.forEach(function(b){
      var v = porBanco[bucket][b]||0;
      totalPorBanco[b]+=v; linhaTotal+=v;
      html += '<td>'+fmtMoney(v)+'</td>';
    });
    html += '<td>'+fmtMoney(linhaTotal)+'</td></tr>';
  });
  var granTotal = BANKS.reduce(function(s,b){return s+totalPorBanco[b];},0);
  html += '<tr><td>Total</td>'+BANKS.map(function(b){return '<td>'+fmtMoney(totalPorBanco[b])+'</td>';}).join('')+'<td>'+fmtMoney(granTotal)+'</td></tr>';
  html += '</tbody></table>';
  wrap.innerHTML = html;
}

function renderAgingChart(){
  renderAgingLegend();
  renderAgingSvg();
  renderAgingTable();
}

document.getElementById('agingToggleView').addEventListener('click', function(){
  var chartWrap = document.getElementById('agingChartWrap');
  var tableWrap = document.getElementById('agingTableWrap');
  var showingTable = tableWrap.style.display !== 'none';
  chartWrap.style.display = showingTable ? '' : 'none';
  tableWrap.style.display = showingTable ? 'none' : '';
  this.textContent = showingTable ? 'Ver como tabela' : 'Ver como gráfico';
});

function referenceKey(item){
  return item.referencia ? ('REF:'+item.referencia) : ('DOC:'+item.doc+':'+item.item);
}

// Agrupa as parcelas ainda a decidir pela referência do documento. Parcelas
// da mesma referência são avaliadas em conjunto: o prazo usa a MÉDIA de dias
// (lançamento -> vencimento) de todas as parcelas do grupo, não o prazo de
// cada parcela isolada — uma parcela acima do prazo máximo não desqualifica
// o grupo se a média ficar dentro do limite. Já o valor máx. por título e os
// limites acumulados (concentração / carteira, pela SOMA das parcelas)
// continuam contaminando o grupo inteiro se qualquer parcela ou a soma estourar.
function buildGroupStats(){
  var groups = {};
  state.itemsToDecide.forEach(function(it){
    var k = referenceKey(it);
    (groups[k] = groups[k] || []).push(it);
  });
  var stats = {};
  Object.keys(groups).forEach(function(k){
    var items = groups[k];
    var sumAmount = items.reduce(function(s,it){ return s+it.amount; }, 0);
    var prazos = items.map(function(it){ return daysBetween(it.postingDate, it.dueDate); }).filter(function(p){ return p!=null; });
    var prazoMedio = prazos.length ? prazos.reduce(function(s,p){return s+p;},0)/prazos.length : null;
    var perBank = {};
    ROUTABLE_BANKS.forEach(function(b){
      var r = state.rules[b];
      perBank[b] = items.some(function(it){
        return r.valor_max_titulo!=null && it.amount>r.valor_max_titulo;
      });
    });
    stats[k] = { items:items, sumAmount:sumAmount, prazoMedio:prazoMedio, perBank:perBank };
  });
  return stats;
}

function evaluate(item, bank){
  var r = state.rules[bank];
  var saldoMap = evaluate._saldoMap || (evaluate._saldoMap = saldoByCustomerBank());
  var groupStats = evaluate._groupStats || (evaluate._groupStats = buildGroupStats());

  var group = groupStats[referenceKey(item)];
  var valorAcumulado = group ? group.sumAmount : item.amount;
  var multiParcela = !!(group && group.items.length>1);

  var saldoAtual = saldoMap[item.customer+'|'+bank] || 0;
  var novoSaldoCliente = saldoAtual + valorAcumulado;
  var prazo = daysBetween(item.postingDate, item.dueDate);
  // a regra de prazo usa a média das parcelas da mesma referência, não o
  // prazo individual do título
  var prazoRegra = (group && group.prazoMedio!=null) ? group.prazoMedio : prazo;
  var concentracaoResultante = r.limite_carteira ? (novoSaldoCliente / r.limite_carteira) : null;
  var sufixoAcum = multiParcela ? ' (acumulado da referência)' : '';

  // Elegibilidade = APENAS prazo (data) e concentração por sacado. O limite
  // da carteira não entra aqui — ele é um piso a alcançar, não um teto que
  // bloqueia elegibilidade (isso é usado depois no cálculo de gap/sugestão).
  var checks = [];
  if(r.prazo_max!=null && prazoRegra!=null){
    var prazoLabel = multiParcela ? 'Prazo médio da referência' : 'Prazo';
    var prazoDetalhe = (multiParcela ? Math.round(prazoRegra*10)/10+'d médio' : prazoRegra+'d')+' / máx '+r.prazo_max+'d';
    checks.push({label:prazoLabel, ok:prazoRegra<=r.prazo_max, detail:prazoDetalhe});
  }
  if(r.concentracao!=null && r.limite_carteira!=null) checks.push({label:'Concentração'+sufixoAcum, ok:concentracaoResultante<=r.concentracao, detail:fmtPct(concentracaoResultante)+' / máx '+fmtPct(r.concentracao)});
  if(multiParcela && group.perBank[bank]){
    checks.push({label:'Parcelas da referência', ok:false, detail:'outra parcela da referência excede valor máx. do título'});
  }

  var eligible = checks.every(function(c){return c.ok;});
  return {eligible:eligible, checks:checks};
}

// Gap de cada banco roteável = quanto falta pra alcançar o piso da carteira
// (limite_carteira, tratado como MÍNIMO) + o que vence nos próximos 7 dias
// (esse valor vai sair em breve e precisa ser reposto). Banco com maior gap
// é priorizado na sugestão — é uma métrica por banco, não por título.
function bankGaps(){
  if(bankGaps._cache) return bankGaps._cache;
  var totals = totalByBank();
  var gaps = {};
  ROUTABLE_BANKS.forEach(function(b){
    var r = state.rules[b];
    var atual = totals[b] || 0;
    var faltaPiso = r.limite_carteira!=null ? Math.max(r.limite_carteira - atual, 0) : 0;
    var vencendo7 = VENC7_POR_BANCO[b] || 0;
    gaps[b] = faltaPiso + vencendo7;
  });
  bankGaps._cache = gaps;
  return gaps;
}

// Cada linha da tabela de resultado passa a ser uma REFERÊNCIA (agrupando
// todas as parcelas dela), não mais uma parcela isolada. O agrupamento em si
// (buildGroupStats) já roda sobre TODAS as parcelas a decidir, sem filtro,
// pra elegibilidade acumulada continuar correta — os filtros abaixo só
// decidem quais referências aparecem na tabela.
function getFilteredGroups(){
  var from = document.getElementById('fDateFrom').value || null;
  var to = document.getElementById('fDateTo').value || null;
  var bankFilter = document.getElementById('fBank').value;

  var groupStats = evaluate._groupStats || (evaluate._groupStats = buildGroupStats());
  var rows = [];
  Object.keys(groupStats).forEach(function(key){
    var group = groupStats[key];
    var items = group.items;
    var rep = items[0];

    if(from && rep.postingDate < from) return;
    if(to && rep.postingDate > to) return;

    // pro check "Valor do título" usar o pior caso (maior parcela do grupo),
    // já que o valor acumulado/prazo médio o evaluate() já busca pelo grupo
    var maxAmount = items.reduce(function(m,it){ return Math.max(m,it.amount); }, 0);
    var evalItem = {customer:rep.customer, postingDate:rep.postingDate, dueDate:rep.dueDate,
      amount:maxAmount, referencia:rep.referencia, doc:rep.doc, item:rep.item};

    var results = {};
    ROUTABLE_BANKS.forEach(function(b){ results[b]=evaluate(evalItem,b); });
    if(bankFilter && !(results[bankFilter] && results[bankFilter].eligible)) return;
    var gaps = bankGaps();
    var eligibleBanks = ROUTABLE_BANKS.filter(function(b){ return results[b].eligible; });
    eligibleBanks.sort(function(a,b){ return gaps[b] - gaps[a]; });

    var dues = items.map(function(it){ return it.dueDate; }).filter(Boolean).sort();
    rows.push({
      key:key, items:items, customer:rep.customer, customerName:rep.customerName||'', postingDate:rep.postingDate,
      dueMin: dues.length?dues[0]:null, dueMax: dues.length?dues[dues.length-1]:null,
      qtd: items.length, valorTotal: group.sumAmount, prazoMedio: group.prazoMedio,
      referenciaLabel: rep.referencia || ('Doc '+rep.doc+(rep.item?('-'+rep.item):'')),
      results: results, suggestion: eligibleBanks.length?eligibleBanks[0]:null
    });
  });
  rows.sort(function(a,b){ return b.valorTotal - a.valorTotal; });
  return rows;
}

function renderResults(){
  evaluate._saldoMap = null; evaluate._totals = null; evaluate._groupStats = null; bankGaps._cache = null;
  var rowsData = getFilteredGroups();
  var onlyEligible = document.getElementById('fOnlyEligible').checked;
  if(onlyEligible) rowsData = rowsData.filter(function(r){ return r.suggestion; });

  var totalValor = rowsData.reduce(function(s,r){return s+r.valorTotal;},0);
  var totalParcelas = rowsData.reduce(function(s,r){return s+r.qtd;},0);
  var comSugestao = rowsData.filter(function(r){return r.suggestion;}).length;
  document.getElementById('summaryStrip').innerHTML =
    '<span><b>'+rowsData.length+'</b>referências filtradas</span>'+
    '<span><b>'+totalParcelas+'</b>parcelas</span>'+
    '<span><b>'+fmtMoney(totalValor)+'</b>em valor</span>'+
    '<span><b>'+comSugestao+'</b>com sugestão</span>'+
    '<span><b>'+(rowsData.length-comSugestao)+'</b>sem banco elegível</span>'+
    '<span><b>'+state.itemsPreAssigned.length+'</b>já direcionados (referência)</span>';

  var holder = document.getElementById('resultsHolder');
  if(!ITENS_ALL.length){ holder.innerHTML='<div class="empty">Nenhum título retornado pelo SAP nesta consulta.</div>'; return; }
  if(!rowsData.length){ holder.innerHTML='<div class="empty">Nenhuma referência encontrada com os filtros atuais.</div>'; return; }

  var html = '<table class="rtb"><thead><tr><th>Referência</th><th>Cliente</th><th>Lançamento</th><th>Vencimento</th>'+
    '<th style="text-align:right">Parcelas</th><th style="text-align:right">Valor total</th><th style="text-align:right">Prazo médio</th>'+
    '<th>Elegibilidade</th><th>Sugestão</th></tr></thead><tbody>';

  rowsData.forEach(function(r,i){
    var chips = ROUTABLE_BANKS.map(function(b){ var res=r.results[b]; return '<span class="chip '+(res.eligible?'ok':'bad')+'">'+b+'</span>'; }).join('');
    var vencTxt = '—';
    if(r.dueMin){ vencTxt = r.dueMin===r.dueMax ? fmtDateBR(r.dueMin) : (fmtDateBR(r.dueMin)+' – '+fmtDateBR(r.dueMax)); }
    html += '<tr class="datarow" data-idx="'+i+'">'+
      '<td>'+esc(r.referenciaLabel)+'</td>'+
      '<td>'+esc(r.customer)+(r.customerName?(' — '+esc(r.customerName)):'')+'</td>'+
      '<td>'+fmtDateBR(r.postingDate)+'</td>'+
      '<td>'+vencTxt+'</td>'+
      '<td class="num">'+r.qtd+'</td>'+
      '<td class="num">'+fmtMoney(r.valorTotal)+'</td>'+
      '<td class="num">'+(r.prazoMedio!=null?(Math.round(r.prazoMedio*10)/10)+'d':'—')+'</td>'+
      '<td>'+chips+'</td>'+
      '<td>'+(r.suggestion?'<span class="suggest-pill">'+r.suggestion+'</span>':'<span class="suggest-pill none">Nenhum elegível</span>')+'</td>'+
      '</tr>';
    if(state.expanded[i]){
      html += '<tr class="detail-row"><td colspan="9"><div class="detail-grid">';
      ROUTABLE_BANKS.forEach(function(b){
        res.checks.forEach(function(c){ html += '<div class="crit'+(c.ok?'':' fail')+'"><span>'+c.label+'</span><span class="v">'+c.detail+'</span></div>'; });
        html += '</div>';
      });
      html += '</div>';
      html += '<div class="ct-sub" style="margin:14px 0 4px">Parcelas desta referência ('+r.items.length+')</div>';
      html += '<table class="sub-table"><thead><tr><th>Documento</th><th>Lançamento</th><th>Vencimento</th><th>Valor</th></tr></thead><tbody>'+
        r.items.map(function(it){
          return '<tr><td>'+esc(it.doc)+(it.item?('-'+esc(it.item)):'')+'</td><td>'+fmtDateBR(it.postingDate)+'</td><td>'+fmtDateBR(it.dueDate)+'</td><td>'+fmtMoney(it.amount)+'</td></tr>';
        }).join('')+
        '</tbody></table>';
      html += '</td></tr>';
    }
  });
  html += '</tbody></table>';
  holder.innerHTML = '<div class="rtb-wrap">'+html+'</div>';

  holder.querySelectorAll('tr.datarow').forEach(function(tr){
    tr.addEventListener('click', function(){
      var idx = tr.getAttribute('data-idx');
      state.expanded[idx] = !state.expanded[idx];
      renderResults();
    });
  });
}

document.getElementById('exportServerBtn').addEventListener('click', function(e){
  e.preventDefault();
  var from = document.getElementById('fDateFrom').value || '';
  var to = document.getElementById('fDateTo').value || '';
  window.location.href = '/export-recebiveis-csv?dtStart='+encodeURIComponent(from)+'&dtEnd='+encodeURIComponent(to);
});

['fDateFrom','fDateTo','fOnlyEligible'].forEach(function(id){
  document.getElementById(id).addEventListener('input', renderResults);
});
document.getElementById('fBank').addEventListener('change', renderResults);

function populateBankFilter(){
  var sel = document.getElementById('fBank');
  ROUTABLE_BANKS.forEach(function(b){
    var opt = document.createElement('option');
    opt.value = b; opt.textContent = b;
    sel.appendChild(opt);
  });
}

function initDefaultDates(){
  function toISO(d){ return d.toISOString().slice(0,10); }
  var to = new Date();
  var from = new Date();
  from.setDate(from.getDate() - 7);
  document.getElementById('fDateTo').value = toISO(to);
  document.getElementById('fDateFrom').value = toISO(from);
}

function renderFidcCards(){
  var holder = document.getElementById('fidcCards');
  var html = '';
  FIDC_LIST.forEach(function(fidc){
    var valor = VALOR_POR_FIDC[fidc] || 0;
    var qtd = QTD_TITULOS_POR_FIDC[fidc] || 0;
    html += '<div class="kp v">';
    html += '<div class="kl"><span>'+esc(fidc)+'</span><span>'+qtd+' título(s)</span></div>';
    html += '<div class="kv">'+fmtMoney(valor)+'</div>';
    html += '<div class="ksub">em aberto direcionado a este FIDC</div>';
    html += '</div>';
  });
  holder.innerHTML = html;
}

function renderRulesTable(){
  var el = document.getElementById('rulesTable');
  var html = '<tr><th>Banco</th><th>Limite da carteira (R$)</th><th>Concentração máx. por sacado (%)</th>'+
             '<th>Valor máx. por título (R$)</th><th>Prazo máx. (dias)</th></tr>';
  BANKS.forEach(function(bank){
    var r = state.rules[bank];
    html += '<tr><td class="bankcell">'+bank+'</td>';
    html += fieldTd(bank,'limite_carteira', r.limite_carteira);
    html += fieldTd(bank,'concentracao', r.concentracao!=null ? r.concentracao*100 : null);
    html += fieldTd(bank,'valor_max_titulo', r.valor_max_titulo);
    html += fieldTd(bank,'prazo_max', r.prazo_max);
    html += '</tr>';
  });
  el.innerHTML = html;
  el.querySelectorAll('input').forEach(function(inp){
    inp.addEventListener('change', function(){
      var bank = inp.getAttribute('data-bank'), field = inp.getAttribute('data-field');
      var raw = inp.value.trim();
      var val = raw==='' ? null : Number(raw);
      if(field==='concentracao' && val!=null) val = val/100;
      state.rules[bank][field] = (val==null || isNaN(val)) ? null : val;
      document.getElementById('saveRulesStatus').textContent = '';
    });
  });
}
function fieldTd(bank, field, val){
  return '<td><input type="number" step="any" data-bank="'+bank+'" data-field="'+field+'" value="'+(val==null?'':val)+'" placeholder="—"></td>';
}

document.getElementById('saveRulesBtn').addEventListener('click', function(){
  var statusEl = document.getElementById('saveRulesStatus');
  statusEl.style.color = 'var(--t2)';
  statusEl.textContent = 'Salvando...';
  fetch('/salvar-regras', {
    method:'POST',
    headers:{'Content-Type':'application/json'},
    body: JSON.stringify(state.rules)
  }).then(function(resp){ return resp.json(); }).then(function(data){
    if(data.ok){
      statusEl.style.color = 'var(--G)';
      statusEl.textContent = 'Salvo! Recarregando a página...';
      setTimeout(function(){ window.location.reload(); }, 700);
    } else {
      statusEl.style.color = 'var(--R)';
      statusEl.textContent = 'Erro ao salvar: '+(data.erro||'desconhecido');
    }
  }).catch(function(e){
    statusEl.style.color = 'var(--R)';
    statusEl.textContent = 'Erro de rede ao salvar: '+e;
  });
});

function renderAll(){ renderOverview(); renderFidcCards(); renderRulesTable(); renderResults(); }
populateBankFilter();
initDefaultDates();
renderAll();
renderAgingChart();
})();
</script>
<script>
// ══════════════════════════════════════════════════════════
// ABA EXCLUSÃO DE TÍTULOS (TODOS OS BANCOS)
// ══════════════════════════════════════════════════════════
const EXC = { titulos: [], selecionados: new Set(), carregado: false, carregando: false };

function excEsc(v) {
  return String(v == null ? '' : v).replace(/[&<>"']/g, c => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}
function excBRL(v) {
  if (v == null || v === '') return '—';
  return 'R$ ' + Number(v).toLocaleString('pt-BR', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}
function excData(iso) {
  if (!iso) return '—';
  const p = String(iso).slice(0, 10).split('-');
  return p.length === 3 ? `${p[2]}/${p[1]}/${p[0]}` : iso;
}

function excCarregar(forcar) {
  if (EXC.carregando || (EXC.carregado && !forcar)) return;
  EXC.carregando = true;
  const tb = document.querySelector('#excTabela tbody');
  tb.innerHTML = '<tr><td colspan="11" class="muted">Carregando títulos e dados do SAP…</td></tr>';
  fetch('/api/titulos-excluiveis')
    .then(r => r.json())
    .then(d => {
      if (!d.ok) throw new Error(d.erro || 'erro ao carregar');
      EXC.titulos = d.titulos || [];
      const ainda = new Set(EXC.titulos.map(t => t.chave));
      EXC.selecionados.forEach(c => { if (!ainda.has(c)) EXC.selecionados.delete(c); });
      EXC.carregado = true;
      excMontarBancos();
      const aviso = document.getElementById('excAviso');
      if (d.processador_rodando) {
        aviso.textContent = 'O processador de boletos está rodando agora: a exclusão fica bloqueada até ele terminar.';
        aviso.style.display = 'block';
      } else if (d.aviso) {
        aviso.textContent = d.aviso; aviso.style.display = 'block';
      } else {
        aviso.style.display = 'none';
      }
      excRender();
      excRenderLog(d.log || []);
    })
    .catch(e => {
      tb.innerHTML = `<tr><td colspan="11" style="color:var(--R)">Erro: ${excEsc(e.message)}</td></tr>`;
    })
    .finally(() => { EXC.carregando = false; });
}

// opções do filtro de banco montadas com os bancos presentes na base
function excMontarBancos() {
  const sel = document.getElementById('excBanco');
  const atual = sel.value;
  const bancos = Array.from(new Set(EXC.titulos.map(t => t.banco).filter(Boolean))).sort();
  sel.innerHTML = '<option value="">Todos os bancos</option>' +
    bancos.map(b => `<option value="${excEsc(b)}">${excEsc(b)}</option>`).join('');
  sel.value = bancos.includes(atual) ? atual : '';
}

function excFiltrados() {
  const banco = document.getElementById('excBanco').value;
  const de = document.getElementById('excDe').value;
  const ate = document.getElementById('excAte').value;
  const busca = document.getElementById('excBusca').value.trim().toUpperCase();
  return EXC.titulos.filter(t => {
    if (banco && t.banco !== banco) return false;
    if (de && (!t.data_processamento || t.data_processamento < de)) return false;
    if (ate && (!t.data_processamento || t.data_processamento > ate)) return false;
    if (busca) {
      const alvo = [t.chave, t.referencia, t.cliente, t.cliente_nome].join(' ').toUpperCase();
      if (!alvo.includes(busca)) return false;
    }
    return true;
  });
}

function excRender() {
  const lista = excFiltrados();
  const tb = document.querySelector('#excTabela tbody');
  if (!lista.length) {
    tb.innerHTML = '<tr><td colspan="11" class="muted">Nenhum título para os filtros escolhidos.</td></tr>';
  } else {
    tb.innerHTML = lista.map(t => {
      const [emp, doc, , item] = t.chave.split('|');
      const marcado = EXC.selecionados.has(t.chave);
      const corBanco = BANK_COLORS[t.banco] || BANK_COLORS["OUTRO"] || '#8AAB94';
      const situacao = !t.encontrado_sap ? '<span class="muted">não encontrado</span>'
        : (t.compensado_em ? `Compensado em ${excData(t.compensado_em)}` : 'Em aberto');
      return `<tr class="${marcado ? 'sel' : ''}">
        <td><input type="checkbox" data-chave="${excEsc(t.chave)}" ${marcado ? 'checked' : ''}
             onchange="excMarcar(this.dataset.chave, this.checked)"></td>
        <td>${excData(t.data_processamento)} <span class="muted">${excEsc(t.hora_processamento)}</span></td>
        <td><span class="tag" style="background:${corBanco}40;color:var(--t);border:1px solid ${corBanco}">${excEsc(t.banco)}</span> <span class="muted">${excEsc(t.house_bank)}</span></td>
        <td>${excEsc(emp)}</td><td>${excEsc(doc)}</td><td>${excEsc(item)}</td>
        <td>${excEsc(t.referencia) || '<span class="muted">—</span>'}</td>
        <td>${excEsc(t.cliente_nome || t.cliente) || '<span class="muted">—</span>'}</td>
        <td class="num">${excBRL(t.valor)}</td>
        <td>${excData(t.vencimento)}</td>
        <td>${situacao}</td>
      </tr>`;
    }).join('');
  }
  document.getElementById('excQtdVis').textContent = lista.length;
  const todos = document.getElementById('excTodos');
  todos.checked = lista.length > 0 && lista.every(t => EXC.selecionados.has(t.chave));
  excAtualizarResumo();
}

function excMarcar(chave, marcado) {
  if (marcado) EXC.selecionados.add(chave); else EXC.selecionados.delete(chave);
  excRender();
}
function excMarcarTodos(marcado) {
  excFiltrados().forEach(t => { if (marcado) EXC.selecionados.add(t.chave); else EXC.selecionados.delete(t.chave); });
  excRender();
}
function excLimparFiltros() {
  ['excBanco', 'excDe', 'excAte', 'excBusca'].forEach(id => { document.getElementById(id).value = ''; });
  excRender();
}
function excSelecionadosDados() {
  return EXC.titulos.filter(t => EXC.selecionados.has(t.chave));
}
function excAtualizarResumo() {
  const sel = excSelecionadosDados();
  const total = sel.reduce((s, t) => s + (Number(t.valor) || 0), 0);
  document.getElementById('excQtdSel').textContent = sel.length;
  document.getElementById('excValSel').textContent = excBRL(total);
  document.getElementById('excBtnExcluir').disabled = sel.length === 0;
}

function excAbrirModal() {
  const sel = excSelecionadosDados();
  if (!sel.length) return;
  const total = sel.reduce((s, t) => s + (Number(t.valor) || 0), 0);
  const bancos = [...new Set(sel.map(t => t.banco))].join(' e ');
  document.getElementById('excModalResumo').innerHTML =
    `Você vai remover <b>${sel.length}</b> título(s) do ${excEsc(bancos)} (${excBRL(total)}) da base de processados. ` +
    `Eles poderão ser criados de novo na próxima execução, se HouseBank e Atribuição forem alterados no SAP.`;
  document.getElementById('excErro').textContent = '';
  document.getElementById('excSenha').value = '';
  document.getElementById('excModal').style.display = 'flex';
  document.getElementById('excNome').focus();
}
function excFecharModal() {
  document.getElementById('excModal').style.display = 'none';
  document.getElementById('excSenha').value = '';
}

function excConfirmar() {
  const nome = document.getElementById('excNome').value.trim();
  const senha = document.getElementById('excSenha').value;
  const erro = document.getElementById('excErro');
  if (nome.length < 3) { erro.textContent = 'Informe seu nome.'; return; }
  if (!senha) { erro.textContent = 'Informe a senha.'; return; }
  const btn = document.getElementById('excBtnConfirmar');
  btn.disabled = true; btn.textContent = 'Excluindo…'; erro.textContent = '';
  fetch('/excluir-titulos', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ nome, senha, chaves: [...EXC.selecionados] }),
  })
    .then(r => r.json().then(d => ({ status: r.status, d })))
    .then(({ d }) => {
      if (!d.ok) { erro.textContent = d.erro || 'Falha na exclusão.'; return; }
      excFecharModal();
      EXC.selecionados.clear();
      const aviso = document.getElementById('excAviso');
      aviso.textContent = `${d.removidas} título(s) excluído(s) da base. Backup: ${d.backup}.`;
      aviso.style.display = 'block';
      excCarregar(true);
    })
    .catch(e => { erro.textContent = 'Erro de comunicação: ' + e.message; })
    .finally(() => { btn.disabled = false; btn.textContent = 'Confirmar exclusão'; });
}

function excRenderLog(log) {
  const tb = document.querySelector('#excLog tbody');
  if (!log.length) { tb.innerHTML = '<tr><td colspan="7" class="muted">Nenhuma exclusão registrada.</td></tr>'; return; }
  tb.innerHTML = log.map(r => `<tr>
    <td>${excEsc(r.data_hora)}</td><td>${excEsc(r.nome)}</td><td>${excEsc(r.banco)}</td>
    <td>${excEsc(r.chave_titulo)}</td><td>${excEsc(r.referencia) || '—'}</td>
    <td>${excEsc(r.cliente) || '—'}</td><td class="num">${r.valor ? excBRL(r.valor) : '—'}</td></tr>`).join('');
}

// carrega a aba na primeira vez que ela for aberta
(function () {
  const showTabOriginal = window.showTab;
  window.showTab = function (nome) {
    showTabOriginal(nome);
    if (nome === 'exclusao') excCarregar(false);
  };
  document.addEventListener('keydown', e => {
    const modal = document.getElementById('excModal');
    if (modal.style.display !== 'flex') return;
    if (e.key === 'Escape') excFecharModal();
    if (e.key === 'Enter') excConfirmar();
  });
})();
</script>
</body>
</html>
"""

    return render_template_string(
        html_template,
        abertos_json=abertos_json,
        processados_json=processados_json,
        historico_json=historico_json,
        qtd_ajustar=qtd_ajustar,
        historico_ajuste_json=historico_ajuste_json,
        vencimento_json=vencimento_json,
        banks=BANKS,
        routable_banks=ROUTABLE_BANKS,
        bank_rules_json=json.dumps(BANK_RULES),
        saldo_json=json.dumps(dados_tit["saldo"]),
        itens_json=json.dumps(dados_tit["itens"]),
        venc7_json=json.dumps(dados_tit["venc7PorBanco"]),
        venc15_json=json.dumps(dados_tit["venc15PorBanco"]),
        venc30_json=json.dumps(dados_tit["venc30PorBanco"]),
        qtd_titulos_json=json.dumps(dados_tit["qtdTitulosPorBanco"]),
        valor_fidc_json=json.dumps(dados_tit["valorPorFidc"]),
        qtd_fidc_json=json.dumps(dados_tit["qtdTitulosPorFidc"]),
        valor_carteira_simples_abc=dados_tit["valorCarteiraSimplesAbc"],
        qtd_carteira_simples_abc=dados_tit["qtdCarteiraSimplesAbc"],
        fidc_list_json=json.dumps(FIDC_LABELS),
        aging_json=json.dumps({"buckets": dados_tit["agingBuckets"], "porBanco": dados_tit["agingPorBanco"]}),
        vencido_json=json.dumps(dados_tit["vencidoPorBanco"]),
        atualizado_em_tit=dados_tit["atualizadoEm"],
        erro_saldo=dados_tit["erroSaldo"],
        erro_itens=dados_tit["erroItens"],
        erro_aging=dados_tit["erroAging"],
    )




# ============================================================
# 5B. ROTAS — Títulos nos bancos
# ============================================================

@app.route("/salvar-regras", methods=["POST"])
def salvar_regras():
    """Recebe as regras editadas na tela e grava definitivamente em
    regras_bancos.csv — a partir daí valem pra qualquer um que abrir a
    página, não só pra quem editou."""
    global BANK_RULES
    try:
        payload = request.get_json(force=True, silent=False) or {}
        novas_regras = {}
        for banco in BANKS:
            dados_banco = payload.get(banco, {}) or {}
            prazo = _num_ou_none(dados_banco.get("prazo_max"))
            novas_regras[banco] = {
                "limite_carteira": _num_ou_none(dados_banco.get("limite_carteira")),
                "concentracao": _num_ou_none(dados_banco.get("concentracao")),
                "valor_max_titulo": _num_ou_none(dados_banco.get("valor_max_titulo")),
                "prazo_max": int(prazo) if prazo is not None else None,
            }
        salvar_regras_bancos(novas_regras)
        BANK_RULES = novas_regras
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "erro": str(e)}), 400


@app.route("/export-recebiveis-csv")
def export_recebiveis_csv():
    """Exporta o resultado (títulos a decidir + sugestão) filtrado por
    data de lançamento, usando as regras salvas em regras_bancos.csv."""
    global BANK_RULES
    BANK_RULES = carregar_regras_bancos()
    dt_start = request.args.get("dtStart", "")
    dt_end = request.args.get("dtEnd", "")

    dados = obter_dados_recebiveis()
    saldo_map = calcular_saldo_por_cliente_banco(dados)
    totais = calcular_total_por_banco(dados)

    grupos = calcular_grupos_referencia(dados)
    grupo_valor = {k: sum(i["amount"] for i in v) for k, v in grupos.items()}
    grupo_falhas = {k: {b: _grupo_falha_individual(v, b) for b in ROUTABLE_BANKS} for k, v in grupos.items()}
    grupo_prazo_medio = {k: _grupo_prazo_medio(v) for k, v in grupos.items()}

    linhas = []
    for item in dados["itens"]:
        if item["preAssignedBank"]:
            continue
        if dt_start and item["postingDate"] < dt_start:
            continue
        if dt_end and item["postingDate"] > dt_end:
            continue
        key = _referencia_key(item)
        valor_acumulado = grupo_valor.get(key, item["amount"])
        falhas_grupo = grupo_falhas.get(key, {})
        prazo_medio = grupo_prazo_medio.get(key)
        sugestao, elegiveis = sugerir_banco(item, saldo_map, totais, dados["venc7PorBanco"], valor_acumulado, falhas_grupo, prazo_medio)
        _, prazo = avaliar_titulo(item, ROUTABLE_BANKS[0], saldo_map, totais, valor_acumulado, falhas_grupo.get(ROUTABLE_BANKS[0], False), prazo_medio)
        linhas.append({
            "Documento": item["doc"],
            "Item": item["item"],
            "Cliente": item["customer"],
            "Lancamento": item["postingDate"],
            "Vencimento": item["dueDate"],
            "Valor": item["amount"],
            "PrazoDias": prazo,
            "BancosElegiveis": "/".join(elegiveis),
            "Sugestao": sugestao or "",
        })

    df = pd.DataFrame(linhas)
    buf = io.BytesIO()
    df.to_csv(buf, index=False, sep=";", decimal=",", encoding="utf-8-sig")
    buf.seek(0)
    return send_file(
        buf,
        mimetype="text/csv",
        as_attachment=True,
        download_name="/var/www/api_cobranca/roteador_recebiveis.csv",
    )


# ============================================================
# 6. EXPORTAÇÃO EXCEL
# ============================================================

@app.route("/export-cr-excel")
def export_cr_excel():
    dt_start = request.args.get("dtStart", "")
    dt_end   = request.args.get("dtEnd",   "")

    abertos, processados, _, _, _ = obter_dados_cr()

    df_a = pd.DataFrame(abertos)
    df_p = pd.DataFrame(processados)

    if not df_a.empty and "Date" in df_a.columns and dt_start and dt_end:
        df_a = df_a[(df_a["Date"] >= dt_start) & (df_a["Date"] <= dt_end)]

    if not df_p.empty and "Data_Processamento" in df_p.columns and dt_start and dt_end:
        df_p = df_p[
            (df_p["Data_Processamento"] >= dt_start)
            & (df_p["Data_Processamento"] <= dt_end)
        ]

    output = io.BytesIO()
    with pd.ExcelWriter(output, engine="xlsxwriter") as writer:
        df_a.to_excel(writer, sheet_name="Títulos_Abertos",     index=False)
        df_p.to_excel(writer, sheet_name="Títulos_Processados", index=False)

    output.seek(0)

    return send_file(
        output,
        download_name=(
            f"Contas_Receber_S4HANA_{datetime.now().strftime('%Y%m%d')}.xlsx"
        ),
        as_attachment=True,
        mimetype=(
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        ),
    )


# ============================================================
# 6B. EXCLUSÃO DE TÍTULOS DA BASE (titulos_processados.csv)
#
# Aba "Exclusão de Títulos": lista os títulos já criados em TODOS os
# bancos (mais recentes primeiro) e permite tirar linhas do
# titulos_processados.csv, para que o processador possa criá-los de
# novo em outro banco. Exige nome + senha e grava um log.
#
# Parâmetros (parametros_prod.txt):
#   DASH_EXCLUSAO_SENHA_HASH   hash da senha (werkzeug). Gere com:
#       venv/bin/python -c "from werkzeug.security import generate_password_hash as g; print(g('SUA_SENHA'))"
# Variáveis de ambiente opcionais:
#   DASHBOARD_CSV_PROCESSADOS  (padrão /var/www/api_cobranca/titulos_processados.csv)
#   EXCLUSAO_LOG_PATH          (padrão /var/www/api_cobranca/exclusoes_titulos_log.csv)
#   EXCLUSAO_BACKUP_DIR        (padrão /var/www/api_cobranca/backups_titulos_processados)
#   EXCLUSAO_PREFIXOS          (padrão vazio = todos os bancos; ex.: SFR,SOF limita)
# ============================================================

import subprocess
import threading
import time as _time

from werkzeug.security import check_password_hash

try:
    import fcntl  # Linux: trava de arquivo
except ImportError:  # Windows (desenvolvimento): sem trava
    fcntl = None

PROCESSADOS_CSV_PATH = os.environ.get(
    "DASHBOARD_CSV_PROCESSADOS", "/var/www/api_cobranca/titulos_processados.csv")
EXCLUSAO_LOG_PATH = os.environ.get(
    "EXCLUSAO_LOG_PATH", "/var/www/api_cobranca/exclusoes_titulos_log.csv")
EXCLUSAO_BACKUP_DIR = os.environ.get(
    "EXCLUSAO_BACKUP_DIR", "/var/www/api_cobranca/backups_titulos_processados")
# vazio = todos os bancos (padrão). Se a variável for preenchida (ex.: SFR,SOF),
# a aba volta a listar/excluir só esses prefixos de HouseBank.
EXCLUSAO_PREFIXOS = tuple(
    p.strip().upper() for p in os.environ.get("EXCLUSAO_PREFIXOS", "").split(",") if p.strip())
EXCLUSAO_NOMES_BANCO = {"SFR": "SAFRA", "SOF": "SOFISA", "ABC": "ABC",
                        "ITA": "ITAU", "SAN": "SANTANDER"}


def _banco_exclusao(house_bank):
    """Nome do banco a partir do HouseBank (BB0..BB4 → BANCO DO BRASIL)."""
    prefixo = str(house_bank or "").strip()[:3].upper()
    if prefixo.startswith("BB"):
        return "BANCO DO BRASIL"
    return EXCLUSAO_NOMES_BANCO.get(prefixo, prefixo or "OUTRO")


def _banco_exclusao_permitido(house_bank):
    return (not EXCLUSAO_PREFIXOS
            or str(house_bank or "").strip()[:3].upper() in EXCLUSAO_PREFIXOS)
EXCLUSAO_SERVICO_PROCESSADOR = "api_cobranca.service"
EXCLUSAO_BACKUPS_MANTIDOS = 60

EXCLUSAO_LOG_CAMPOS = ["data_hora", "nome", "ip", "chave_titulo", "banco", "house_bank",
                       "data_processamento", "referencia", "cliente", "valor", "vencimento"]

_exclusao_lock = threading.Lock()
_tentativas_senha = {}          # ip -> [falhas, bloqueado_ate]
_cache_sap_exclusao = {"ts": 0.0, "dados": {}}
_CACHE_SAP_SEGUNDOS = 300


def _parametros_dash():
    return carregar_parametros(os.environ.get(
        "DASHBOARD_PARAM_FILE", "/var/www/api_cobranca/parametros_prod.txt"))


# ---------- leitura / gravação do titulos_processados.csv ----------

def _ler_processados_bruto():
    """Lê o CSV preservando o formato original (BOM, quebra de linha,
    cabeçalho e todas as colunas). Devolve um dict com as linhas."""
    with open(PROCESSADOS_CSV_PATH, "rb") as f:
        bruto = f.read()

    tem_bom = bruto.startswith(b"\xef\xbb\xbf")
    texto = bruto.decode("utf-8-sig")
    quebra = "\r\n" if "\r\n" in texto else "\n"
    linhas = list(csv.reader(io.StringIO(texto)))
    linhas = [l for l in linhas if any(str(c).strip() for c in l)]

    cabecalho = None
    if linhas and "Chave_Titulo" in [c.strip() for c in linhas[0]]:
        cabecalho = linhas[0]
        linhas = linhas[1:]

    nomes = [c.strip() for c in cabecalho] if cabecalho else ["Chave_Titulo", "HouseBank", "Data_Processamento"]
    idx = {n: (nomes.index(n) if n in nomes else None) for n in ("Chave_Titulo", "HouseBank", "Data_Processamento")}
    if idx["Chave_Titulo"] is None:
        raise ValueError("coluna Chave_Titulo não encontrada no titulos_processados.csv")

    return {"bom": tem_bom, "quebra": quebra, "cabecalho": cabecalho, "linhas": linhas, "idx": idx}


def _campo(linha, i):
    return str(linha[i]).strip() if i is not None and i < len(linha) else ""


def _gravar_processados(conteudo):
    """Regrava o arquivo NO MESMO LUGAR (mesmo inode): mantém dono e
    permissões, para o processador (www-data) continuar gravando nele."""
    saida = io.StringIO()
    escritor = csv.writer(saida, lineterminator=conteudo["quebra"])
    if conteudo["cabecalho"]:
        escritor.writerow(conteudo["cabecalho"])
    escritor.writerows(conteudo["linhas"])
    dados = saida.getvalue().encode("utf-8-sig" if conteudo["bom"] else "utf-8")

    with open(PROCESSADOS_CSV_PATH, "r+b") as f:
        f.seek(0)
        f.write(dados)
        f.truncate()
        f.flush()
        os.fsync(f.fileno())


def _backup_processados():
    os.makedirs(EXCLUSAO_BACKUP_DIR, exist_ok=True)
    destino = os.path.join(
        EXCLUSAO_BACKUP_DIR, f"titulos_processados_{datetime.now():%Y%m%d_%H%M%S}.csv")
    with open(PROCESSADOS_CSV_PATH, "rb") as origem, open(destino, "wb") as copia:
        copia.write(origem.read())

    backups = sorted(
        os.path.join(EXCLUSAO_BACKUP_DIR, n) for n in os.listdir(EXCLUSAO_BACKUP_DIR)
        if n.startswith("titulos_processados_") and n.endswith(".csv"))
    for antigo in backups[:-EXCLUSAO_BACKUPS_MANTIDOS]:
        try:
            os.remove(antigo)
        except OSError:
            pass
    return destino


class _TravaArquivo:
    """Trava exclusiva enquanto o CSV é reescrito (Linux)."""

    def __init__(self, caminho):
        self.caminho = caminho + ".lock"
        self.f = None

    def __enter__(self):
        if fcntl is not None:
            self.f = open(self.caminho, "a")
            fcntl.flock(self.f, fcntl.LOCK_EX)
        return self

    def __exit__(self, *exc):
        if self.f is not None:
            fcntl.flock(self.f, fcntl.LOCK_UN)
            self.f.close()


def _processador_rodando():
    """True se o api_cobranca.service estiver executando agora."""
    try:
        r = subprocess.run(["systemctl", "is-active", EXCLUSAO_SERVICO_PROCESSADOR],
                           capture_output=True, text=True, timeout=5)
        return r.stdout.strip() in ("active", "activating", "reloading")
    except Exception:
        return False


# ---------- dados do SAP (referência, cliente, valor...) ----------

def _dados_sap_por_chave(chaves):
    """Busca no SAP os títulos das chaves informadas (com cache de 5 min)."""
    agora = _time.time()
    cache = _cache_sap_exclusao
    if agora - cache["ts"] > _CACHE_SAP_SEGUNDOS:
        cache["dados"], cache["ts"] = {}, agora

    faltando = [c for c in chaves if c not in cache["dados"]]
    if not faltando:
        return cache["dados"]

    parametros = _parametros_dash()
    url = parametros.get("SAP_1_URL", "")
    usuario = parametros.get("SAP_1_USUARIO", "")
    senha = parametros.get("SAP_1_SENHA", "")
    if not url:
        return cache["dados"]

    documentos = sorted({c.split("|")[1] for c in faltando if c.count("|") == 3})
    select = ("CompanyCode,AccountingDocument,FiscalYear,AccountingDocumentItem,DocumentReferenceID_2,"
              "AmountInCompanyCodeCurrency,NetDueDate,Customer,CustomerName,ClearingDate")

    for i in range(0, len(documentos), 40):
        lote = documentos[i:i + 40]
        filtro = "(" + " or ".join(f"AccountingDocument eq '{d}'" for d in lote) + ") and AccountingDocumentType eq 'RV'"
        try:
            registros = _sap_get(url, usuario, senha, filtro=filtro, select=select)
        except Exception as e:
            print(f"[EXCLUSÃO] erro ao consultar o SAP: {e}")
            continue
        for r in registros:
            chave = "|".join(str(r.get(c, "")).strip() for c in
                             ("CompanyCode", "AccountingDocument", "FiscalYear", "AccountingDocumentItem"))
            try:
                valor = float(r.get("AmountInCompanyCodeCurrency") or 0)
            except (TypeError, ValueError):
                valor = 0.0
            cache["dados"][chave] = {
                "referencia": str(r.get("DocumentReferenceID_2") or "").strip(),
                "cliente": str(r.get("Customer") or "").strip(),
                "cliente_nome": str(r.get("CustomerName") or "").strip(),
                "valor": valor,
                "vencimento": formatar_data_sap_iso(r.get("NetDueDate")) or "",
                "compensado_em": formatar_data_sap_iso(r.get("ClearingDate")) or "",
            }
    return cache["dados"]


def _titulos_excluiveis(conteudo):
    """Linhas do CSV dos bancos permitidos (todos, salvo EXCLUSAO_PREFIXOS)."""
    idx = conteudo["idx"]
    titulos = []
    for linha in conteudo["linhas"]:
        house_bank = _campo(linha, idx["HouseBank"])
        if not _banco_exclusao_permitido(house_bank):
            continue
        dt_raw = _campo(linha, idx["Data_Processamento"])
        partes = dt_raw.split()
        titulos.append({
            "chave": _campo(linha, idx["Chave_Titulo"]),
            "house_bank": house_bank,
            "banco": _banco_exclusao(house_bank),
            "data_processamento": (formatar_data_sap_iso(partes[0]) or "") if partes else "",
            "hora_processamento": partes[1] if len(partes) > 1 else "",
            "data_processamento_raw": dt_raw,
        })
    return titulos


def _ler_log_exclusoes(limite=50):
    if not os.path.exists(EXCLUSAO_LOG_PATH):
        return []
    with open(EXCLUSAO_LOG_PATH, "r", encoding="utf-8-sig", newline="") as f:
        linhas = list(csv.DictReader(f))
    return list(reversed(linhas))[:limite]


def _gravar_log_exclusoes(registros):
    novo = not os.path.exists(EXCLUSAO_LOG_PATH)
    with open(EXCLUSAO_LOG_PATH, "a", encoding="utf-8", newline="") as f:
        escritor = csv.DictWriter(f, fieldnames=EXCLUSAO_LOG_CAMPOS)
        if novo:
            escritor.writeheader()
        escritor.writerows(registros)


def _avisar_slack_exclusao(nome, registros):
    """Aviso opcional no Slack (mesmos parâmetros do processador)."""
    p = _parametros_dash()
    url, canal, token = p.get("SLACK_URL"), p.get("SLACK_CANAL"), p.get("SLACK_TOKEN")
    if not (url and canal and token):
        return
    linhas = "\n".join(
        f"• {r['banco']} | {r['chave_titulo']} | ref {r['referencia'] or '-'} | {r['cliente'] or '-'}"
        for r in registros[:30])
    extra = f"\n… e mais {len(registros) - 30}" if len(registros) > 30 else ""
    texto = (f"*Títulos excluídos da base (titulos_processados)*\n"
             f"Por: *{nome}* em {datetime.now():%d/%m/%Y %H:%M}\n\n{linhas}{extra}\n\n"
             f"_Para reprocessar em outro banco: alterar HouseBank e Atribuição no SAP "
             f"e baixar o boleto no banco de origem, se ele foi registrado._")
    try:
        requests.post(url, headers={"Authorization": f"Bearer {token}"},
                      json={"channel": canal, "text": texto}, timeout=15)
    except Exception as e:
        print(f"[EXCLUSÃO] aviso no Slack falhou: {e}")


# ---------- rotas ----------

@app.route("/api/titulos-excluiveis")
def api_titulos_excluiveis():
    try:
        if not os.path.exists(PROCESSADOS_CSV_PATH):
            return jsonify({"ok": True, "titulos": [], "log": _ler_log_exclusoes(),
                            "aviso": "titulos_processados.csv não encontrado"})
        conteudo = _ler_processados_bruto()
        titulos = _titulos_excluiveis(conteudo)
        sap = _dados_sap_por_chave([t["chave"] for t in titulos])
        for t in titulos:
            info = sap.get(t["chave"], {})
            t.update({
                "referencia": info.get("referencia", ""),
                "cliente": info.get("cliente", ""),
                "cliente_nome": info.get("cliente_nome", ""),
                "valor": info.get("valor"),
                "vencimento": info.get("vencimento", ""),
                "compensado_em": info.get("compensado_em", ""),
                "encontrado_sap": bool(info),
            })
        titulos.sort(key=lambda t: (t["data_processamento"], t["hora_processamento"]), reverse=True)
        return jsonify({"ok": True, "titulos": titulos, "log": _ler_log_exclusoes(),
                        "processador_rodando": _processador_rodando()})
    except Exception as e:
        return jsonify({"ok": False, "erro": str(e)}), 500


@app.route("/excluir-titulos", methods=["POST"])
def excluir_titulos():
    ip = request.headers.get("X-Forwarded-For", request.remote_addr or "").split(",")[0].strip()
    payload = request.get_json(force=True, silent=True) or {}
    nome = str(payload.get("nome", "")).strip()
    senha = str(payload.get("senha", ""))
    chaves = [str(c).strip() for c in (payload.get("chaves") or []) if str(c).strip()]

    # bloqueio por tentativas erradas (5 erros -> 15 minutos)
    falhas, bloqueado_ate = _tentativas_senha.get(ip, [0, 0.0])
    if bloqueado_ate > _time.time():
        minutos = int((bloqueado_ate - _time.time()) // 60) + 1
        return jsonify({"ok": False, "erro": f"Muitas tentativas. Tente novamente em {minutos} min."}), 429

    if len(nome) < 3:
        return jsonify({"ok": False, "erro": "Informe seu nome."}), 400
    if not chaves:
        return jsonify({"ok": False, "erro": "Nenhum título selecionado."}), 400

    hash_senha = _parametros_dash().get("DASH_EXCLUSAO_SENHA_HASH", "")
    if not hash_senha:
        return jsonify({"ok": False, "erro": "Senha de exclusão não configurada no servidor "
                                              "(DASH_EXCLUSAO_SENHA_HASH)."}), 500
    if not check_password_hash(hash_senha, senha):
        falhas += 1
        _tentativas_senha[ip] = [falhas, _time.time() + 900 if falhas >= 5 else 0.0]
        _time.sleep(1)
        return jsonify({"ok": False, "erro": "Senha incorreta."}), 403
    _tentativas_senha.pop(ip, None)

    if _processador_rodando():
        return jsonify({"ok": False, "erro": "O processador de boletos está rodando agora. "
                                              "Aguarde ele terminar e tente de novo."}), 409

    with _exclusao_lock, _TravaArquivo(PROCESSADOS_CSV_PATH):
        conteudo = _ler_processados_bruto()
        permitidos = {t["chave"]: t for t in _titulos_excluiveis(conteudo)}

        invalidas = [c for c in chaves if c not in permitidos]
        if invalidas:
            return jsonify({"ok": False, "erro": "Títulos não encontrados na base ou de banco não permitido: "
                                                  + ", ".join(invalidas[:10])}), 400

        backup = _backup_processados()
        selecionadas = set(chaves)
        idx = conteudo["idx"]
        antes = len(conteudo["linhas"])
        conteudo["linhas"] = [
            l for l in conteudo["linhas"]
            if not (_campo(l, idx["Chave_Titulo"]) in selecionadas
                    and _banco_exclusao_permitido(_campo(l, idx["HouseBank"])))
        ]
        removidas = antes - len(conteudo["linhas"])
        _gravar_processados(conteudo)

    sap = _cache_sap_exclusao["dados"]
    agora = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    registros = []
    for c in chaves:
        t, info = permitidos[c], sap.get(c, {})
        registros.append({
            "data_hora": agora, "nome": nome, "ip": ip, "chave_titulo": c,
            "banco": t["banco"], "house_bank": t["house_bank"],
            "data_processamento": t["data_processamento_raw"],
            "referencia": info.get("referencia", ""),
            "cliente": info.get("cliente_nome") or info.get("cliente", ""),
            "valor": f"{info['valor']:.2f}" if info.get("valor") is not None else "",
            "vencimento": info.get("vencimento", ""),
        })
    _gravar_log_exclusoes(registros)
    _avisar_slack_exclusao(nome, registros)

    print(f"[EXCLUSÃO] {nome} ({ip}) removeu {removidas} linha(s); backup em {backup}")
    return jsonify({"ok": True, "removidas": removidas, "backup": os.path.basename(backup)})


# ============================================================
# 7. EXECUÇÃO
# ============================================================

if __name__ == "__main__":
    from waitress import serve
    serve(app, host="0.0.0.0", port=8011)