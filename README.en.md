# Banking integrations with SAP S/4HANA

[🇧🇷 Português](README.md) | 🇺🇸 English

**▶️ [Open the demos](https://joao-2003.github.io/sap-banking-integrations/)**, with fictitious data and nothing to install:
- [Accounts Payable · DDA injection](https://joao-2003.github.io/sap-banking-integrations/demo-remessas.html): the real Streamlit app running in the browser (Pyodide + stlite). Load the sample payment file, process the DDA and route the payments without a boleto to wire transfer or PIX; the first load takes 20 to 60 s.
- [Accounts Receivable · portfolio dashboard](https://joao-2003.github.io/sap-banking-integrations/docs/painel-carteira.html): the Flask dashboard's HTML with made-up items.

The interface is in Portuguese.

A set of automations connecting SAP S/4HANA Public Cloud to five Brazilian banks, on both sides of cash management:

- **Accounts Receivable (`cobranca/`):** registers boletos (Brazilian bank payment slips) for open items through the collection APIs of Banco do Brasil, Santander and Banco ABC, generates CNAB files for Safra and Sofisa, adjusts customer installments according to the invoice and tracks the portfolio on a dashboard.
- **Accounts Payable (`remessas/`):** a Streamlit app that fixes SAP payment files (boleto/DDA, wire transfer and PIX) before they go to the bank, applying bank details and per-payee exceptions.

> I built this for a Brazilian manufacturer, where it runs in production. This repository holds the code structure: tax IDs, bank agreements, branches, accounts, number ranges, addresses, IDs and credentials were removed or replaced with fictitious values. Secrets live outside the repository: `parametros_prod.txt` for collections (see `cobranca/parametros_exemplo.txt`) and `.streamlit/secrets.toml` or environment variables for payment files.

## Accounts Receivable

| File | Purpose |
|---|---|
| `processador_bb_prod.py` | Scheduled job: fetches the day's open items from SAP, picks the bank by house bank and registers the boleto through the bank's API, never twice (each processed item is recorded); final report on Slack |
| `criar_boleto_BB.py` | Banco do Brasil collection API (OAuth2, one agreement per company) |
| `criar_boleto_SANTANDER.py` + `boleto_pdf_santander.py` | Santander API with an e-CNPJ certificate (mTLS); since the bank returns no PDF, the boleto is drawn with ReportLab after validating the barcode, check digits and typeable line |
| `criar_boleto_ABC.py` | Banco ABC API, with a TLS adapter for the legacy renegotiation the bank's server requires |
| `criar_boleto_SAFRA.py`, `criar_boleto_SOFISA.py` | CNAB file generation (Safra and Sofisa), with check digit calculation within the number range assigned by the bank |
| `slack_trigger_safra.py`, `slack_trigger_sofisa.py` | Slack Socket Mode triggers: typing "Gerar Safra" or "Gerar Sofisa" in the channel starts the generation, with a lock against double runs |
| `slack_zip_boletos.py` | Sends the zipped boleto PDFs to Slack |
| `ajustar_parcelas_clientes.py` | Compares SAP installments with the invoice XML from Pipefy and adjusts them through a SOAP service |
| `dashboard_2.py` | Flask portfolio dashboard: aging, due dates, concentration and limits per bank, routing of items to discounting banks |

## Accounts Payable (DDA and payment files)

| File | Purpose |
|---|---|
| `app.py` | Streamlit UI: upload the SAP payment file, review exceptions and download the corrected file |
| `processador_BOLETO.py` | Matches DDA boletos with SAP items and injects the data into the file |
| `processador_TED.py` | Fixes payee bank, branch, account and check digits in the CNAB 240 layout |
| `processador_PIX.py` | Builds PIX payment segments with key type and key per payee |
| `gerar_exemplo.py` | Fictitious CNAB 240 file and DDA response for demo mode (with valid check digits) |

Without `SAP_PASS` (or in the browser) the app runs in demo mode: the SAP DDA query is replaced by the boletos from `gerar_exemplo.py`, and the rest of the processing is unchanged. `tools/gerar_demo_carteira.py` builds the portfolio dashboard page.

## Technical highlights

- Five different bank integrations (REST with OAuth2, mTLS with an A1 certificate and fixed-width CNAB) behind a single job, chosen by the item's house bank.
- Idempotency: each processed item is recorded and never produces a duplicate boleto, so the job can run every 30 minutes.
- Fail loudly on purpose: an unconfigured house bank becomes an error in the report instead of a silently wrong boleto.
- Scheduling with systemd timers and Slack Socket Mode services, with no public endpoint.

## How to run

```bash
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp cobranca/parametros_exemplo.txt cobranca/parametros_prod.txt   # fill it in
cd cobranca && python processador_bb_prod.py                       # issues boletos
streamlit run remessas/app.py                                      # Accounts Payable files
# payment-file credentials: SAP_DDA_URL, SAP_USER and SAP_PASS in .streamlit/secrets.toml or the environment
```

Requires production or sandbox credentials for each bank and an SAP communication user.

## Tech stack

Python 3 · requests · pandas · lxml · Flask · Streamlit · ReportLab · Slack Bolt (Socket Mode) · stlite/Pyodide · SAP S/4HANA (OData, SOAP) · BB, Santander and ABC collection APIs · CNAB 240/400 · systemd

## Author

João Victor Guidi de Lima · [LinkedIn](https://www.linkedin.com/in/joão-lima-8b512) · [GitHub](https://github.com/Joao-2003)
