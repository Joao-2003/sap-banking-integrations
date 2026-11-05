"""Gera docs/painel-carteira.html: o painel Flask da carteira (cobranca/dashboard_2.py) com dados fictícios.

O HTML é o do próprio app, renderizado pelo Flask, mas a consulta OData ao SAP é respondida aqui
com títulos inventados (clientes, valores e datas relativas a hoje) e os CSVs de processamento,
histórico e regras ficam numa pasta temporária. No navegador, um shim de fetch responde às
ações (salvar regras, listar e excluir títulos) sem servidor.

    pip install flask pandas requests openpyxl xlsxwriter
    python tools/gerar_demo_carteira.py
"""
import csv
import json
import os
import random
import sys
import tempfile
from datetime import datetime, timedelta

RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TMP = tempfile.mkdtemp(prefix="demo_carteira_")
rnd = random.Random(2026)
HOJE = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)

# caminhos de trabalho do app apontando para a pasta temporária (antes do import)
for var, nome in [("DASHBOARD_PARAM_FILE", "parametros.txt"), ("DASHBOARD_CSV_PROCESSADOS", "titulos_processados.csv"),
                  ("DASHBOARD_CSV_HISTORICO", "historico_velocidade.csv"), ("REGRAS_CSV_PATH", "regras_bancos.csv"),
                  ("AJUSTE_LOG_PATH", "ajuste_titulos_log.csv"), ("TITULOS_AJUSTADOS_PATH", "titulos_ajustados.csv"),
                  ("TITULOS_CONCILIADOS_ADT_PATH", "titulos_conciliados_adiantamento.csv"),
                  ("EXCLUSAO_LOG_PATH", "exclusoes_titulos_log.csv"), ("EXCLUSAO_BACKUP_DIR", "backups")]:
    os.environ[var] = os.path.join(TMP, nome)
with open(os.environ["DASHBOARD_PARAM_FILE"], "w", encoding="utf-8") as fh:
    fh.write("SAP_1_URL=https://demo.invalid/odata\nSAP_1_USUARIO=demo\nSAP_1_SENHA=demo\n")
with open(os.path.join(RAIZ, "cobranca", "regras_bancos.csv"), encoding="utf-8") as src, \
        open(os.environ["REGRAS_CSV_PATH"], "w", encoding="utf-8") as dst:
    dst.write(src.read())

sys.path.insert(0, os.path.join(RAIZ, "cobranca"))
import dashboard_2 as app_mod  # noqa: E402

CLIENTES = [(f"10{n:05d}", nome) for n, nome in enumerate(
    ["AGRO CAMPO FORTE LTDA", "COMERCIAL TERRA BOA LTDA", "MAQUINAS SEMEAR LTDA", "VERDE VALE PECAS LTDA",
     "COLHEITA CERTA COMERCIO", "RURAL CENTER IMPLEMENTOS", "PLANTAR DISTRIBUIDORA", "HORIZONTE AGRICOLA LTDA",
     "SAFRA NOVA REVENDA LTDA", "CERRADO MAQUINAS S.A.", "SUL AGRO PECAS LTDA", "NORTE CAMPO COMERCIO",
     "VALE DO RIO IMPLEMENTOS", "PLANALTO AGRO LTDA", "SEMENTE BOA COMERCIO", "GRAO DE OURO REVENDA"], start=1)]
REFS = ([""] * 40 + ["ABC-CV"] * 4 + ["ABC"] * 2 + ["SAFRA"] * 2 + ["SOFISA"] * 4 + ["FIDC-ALFA"] * 2
        + ["FIDC-BETA"] * 2 + ["FIDC - GAMA"] * 1 + ["FIDC-DELTA"] * 1 + ["FIDC-EPSILON"] * 1 + ["TED"] * 2
        + ["ENCONTRO DE NF"] * 1)
HOUSEBANKS = [""] * 34 + ["SFR01"] * 4 + ["ABC01"] * 5 + ["SOF01"] * 6 + ["BB001"] * 9 + ["SAN01"] * 4


def ms(d):
    return f"/Date({int(d.timestamp() * 1000)})/"


def titulos():
    regs, processados = [], []
    doc = 90000000
    for _ in range(560):
        criado = HOJE - timedelta(days=rnd.choice([0, 1, 1, 2, 3, 4, 5, 6, 7, 8, 10, 14, 20, 30, 45, 60]))
        if criado.weekday() >= 5:
            criado -= timedelta(days=criado.weekday() - 4)
        doc += rnd.randint(1, 3)
        cli = rnd.choice(CLIENTES)
        parcelas = rnd.choice([1, 2, 3, 3, 4, 5])
        total = rnd.lognormvariate(9.4, 0.9)
        for it in range(1, parcelas + 1):
            venc = criado + timedelta(days=30 * it + rnd.choice([0, 0, 5, 10]))
            if rnd.random() < 0.03:
                venc = criado  # criação == vencimento: entra em "títulos a ajustar"
            aberto = venc >= HOJE - timedelta(days=rnd.choice([0, 3, 12, 25])) or rnd.random() < 0.08
            hb = rnd.choice(HOUSEBANKS)
            ref = rnd.choice(REFS) if not hb else ""
            r = {"CompanyCode": "BR01", "AccountingDocument": str(doc), "FiscalYear": str(criado.year),
                 "AccountingDocumentItem": str(it), "AmountInCompanyCodeCurrency": f"{total / parcelas:.2f}",
                 "Dt_lcto": ms(criado), "PostingDate": ms(criado),
                 "CreationTime": f"PT{rnd.randint(8, 17)}H{rnd.randint(0, 59):02d}M{rnd.randint(0, 59):02d}S",
                 "NetDueDate": ms(venc), "ClearingDate": None if aberto else ms(venc + timedelta(days=rnd.randint(0, 3))),
                 "PostingKey": "01", "Customer": cli[0], "CustomerName": cli[1], "AssignmentReference": ref,
                 "HouseBank": hb, "PaymentMethod": rnd.choice(["", "", "", "", "E", "P"]),
                 "PaymentTerms": rnd.choice(["A002", "Z030", "Z060"]), "DocumentReferenceID_2": ""}
            regs.append(r)
            if hb and rnd.random() < 0.85:  # já virou remessa (processador_bb_prod.py)
                quando = min(criado + timedelta(hours=rnd.uniform(1, 40)), datetime.now() - timedelta(minutes=rnd.randint(5, 300)))
                processados.append(("|".join([r["CompanyCode"], r["AccountingDocument"], r["FiscalYear"], r["AccountingDocumentItem"]]),
                                    hb, quando.strftime("%d/%m/%Y %H:%M:%S")))
    return regs, processados


REGS, PROC = titulos()


def _filtrar(filtro):
    out = REGS
    if "ClearingDate eq null" in filtro:
        out = [r for r in out if r["ClearingDate"] is None]
    if "HouseBank eq ''" in filtro:
        out = [r for r in out if not r["HouseBank"]]
    return out


class _Resp:
    def __init__(self, dados):
        self._d = dados

    def raise_for_status(self):
        return None

    def json(self):
        return self._d


def _get(url, params=None, **_):
    return _Resp({"d": {"results": _filtrar((params or {}).get("$filter", ""))}})


app_mod.requests.get = _get
app_mod.carregar_parametros = lambda *_: {"SAP_1_URL": "https://demo.invalid/odata", "SAP_1_USUARIO": "demo", "SAP_1_SENHA": "demo"}

with open(os.environ["DASHBOARD_CSV_PROCESSADOS"], "w", encoding="utf-8-sig", newline="") as fh:
    w = csv.writer(fh)
    w.writerow(["Chave_Titulo", "HouseBank", "Data_Processamento"])
    w.writerows(PROC)
with open(os.environ["DASHBOARD_CSV_HISTORICO"], "w", encoding="utf-8-sig") as fh:
    fh.write("Data;Delta\n")
    for d in range(60, 0, -1):
        dia = HOJE - timedelta(days=d)
        if dia.weekday() < 5:
            fh.write(f"{dia.strftime('%d/%m/%Y')};{max(0.05, rnd.gauss(0.6 if d > 30 else 0.25, 0.15)):.3f}\n")
with open(os.environ["AJUSTE_LOG_PATH"], "w", encoding="utf-8", newline="") as fh:
    fh.write("data,qtd\n")
    for d in range(30, 0, -1):
        dia = HOJE - timedelta(days=d)
        if dia.weekday() < 5:
            fh.write(f"{dia.strftime('%Y-%m-%d')},{rnd.randint(0, 9)}\n")

SHIM = """<script>
(function(){
  const real = window.fetch.bind(window);
  const DADOS = __EXCLUIVEIS__;   // resposta real de /api/titulos-excluiveis, gerada com os dados fictícios
  const json = (o) => new Response(JSON.stringify(o), {status: 200, headers: {'Content-Type': 'application/json'}});
  window.fetch = async (url, opts) => {
    const u = String(url);
    if (u.includes('/api/titulos-excluiveis')) return json(DADOS);
    if (u.includes('/excluir-titulos')) {
      const corpo = JSON.parse((opts && opts.body) || '{}');
      const chaves = new Set(corpo.chaves || []);
      const agora = new Date().toLocaleString('pt-BR');
      const saem = DADOS.titulos.filter(t => chaves.has(t.chave));
      DADOS.titulos = DADOS.titulos.filter(t => !chaves.has(t.chave));
      DADOS.log = saem.map(t => ({data_hora: agora, nome: corpo.nome, banco: t.banco || t.house_bank || '',
        chave_titulo: t.chave, referencia: t.referencia, cliente: t.cliente_nome || t.cliente, valor: t.valor})).concat(DADOS.log || []);
      return json({ok: true, removidas: saem.length, backup: 'demonstração (nada foi gravado)'});
    }
    return real(url, opts);
  };
  document.addEventListener('click', (e) => {
    const a = e.target.closest('a[href*="export-"]');
    if (a) { e.preventDefault(); alert('Na versão real este botão baixa o arquivo (CSV/Excel). Indisponível na demonstração.'); }
    if (e.target.closest('#saveRulesBtn')) {
      e.stopImmediatePropagation(); e.preventDefault();
      const st = document.getElementById('saveRulesStatus');
      if (st) { st.style.color = 'var(--G)'; st.textContent = 'Regras aplicadas nesta tela. Na versão real são gravadas em regras_bancos.csv.'; }
    }
  }, true);
})();
</script>"""

BANNER = """<div style="position:fixed;left:0;right:0;bottom:0;z-index:9999;background:#f5a623;color:#0c1810;
 font:600 12px/1.6 'JetBrains Mono',monospace;text-align:center;padding:4px 12px">
 Demonstração com dados fictícios · <a href="https://github.com/Joao-2003/sap-banking-integrations" style="color:#0c1810">código no GitHub</a> ·
 <a href="../index.html" style="color:#0c1810">outras demonstrações</a></div>"""


def main():
    cliente = app_mod.app.test_client()
    html = cliente.get("/dashboard-cr").get_data(as_text=True)
    if "Erro ao carregar dados" in html[:400]:
        raise SystemExit(html[:400])
    excl = cliente.get("/api/titulos-excluiveis").get_json()
    excl["titulos"] = excl.get("titulos", [])[:80]
    excl["processador_rodando"] = False
    html = html.replace("<head>", "<head>" + SHIM.replace("__EXCLUIVEIS__", json.dumps(excl)), 1)
    html = html.replace("</body>", BANNER + "</body>")
    os.makedirs(os.path.join(RAIZ, "docs"), exist_ok=True)
    with open(os.path.join(RAIZ, "docs", "painel-carteira.html"), "w", encoding="utf-8") as fh:
        fh.write(html)
    print(f"docs/painel-carteira.html: {len(html) // 1024} KB · {len(REGS)} títulos fictícios")


if __name__ == "__main__":
    main()
