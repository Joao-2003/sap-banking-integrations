# Integrações bancárias com o SAP S/4HANA

🇧🇷 Português | [🇺🇸 English](README.en.md)

**▶️ [Abrir as demonstrações](https://joao-2003.github.io/sap-banking-integrations/)**, com dados fictícios e sem instalar nada:
- [Contas a Pagar · injeção de DDA](https://joao-2003.github.io/sap-banking-integrations/demo-remessas.html): o app Streamlit real rodando no navegador (Pyodide + stlite). Use a remessa de exemplo, processe o DDA e mande os pagamentos sem boleto para TED ou PIX; a primeira carga leva de 20 a 60 s.
- [Contas a Receber · painel da carteira](https://joao-2003.github.io/sap-banking-integrations/docs/painel-carteira.html): o HTML do painel Flask com títulos inventados.

Conjunto de automações que liga o SAP S/4HANA Public Cloud a cinco bancos, nas duas pontas do caixa:

- **Contas a Receber (`cobranca/`):** registra boletos dos títulos em aberto nas APIs de cobrança do Banco do Brasil, Santander e Banco ABC, gera arquivos CNAB para Safra e Sofisa, ajusta parcelas de clientes conforme a nota fiscal e acompanha a carteira em um painel.
- **Contas a Pagar (`remessas/`):** aplicação Streamlit que corrige arquivos de remessa do SAP (boleto/DDA, TED e PIX) antes do envio ao banco, aplicando dados bancários e exceções por favorecido.

> Desenvolvido por mim para uma indústria brasileira e usado em produção. Este repositório traz a estrutura do código: CNPJs, convênios, agências, contas, faixas de nosso número, endereços, IDs e credenciais foram removidos ou trocados por valores fictícios. Os segredos ficam fora do repositório: `parametros_prod.txt` na cobrança (veja `cobranca/parametros_exemplo.txt`) e `.streamlit/secrets.toml` ou variáveis de ambiente nas remessas.

## Contas a Receber

| Arquivo | Função |
|---|---|
| `processador_bb_prod.py` | Rotina agendada: busca no SAP os títulos do dia, escolhe o banco pelo house bank e registra o boleto na API do banco, sem duplicar (cada título processado fica registrado); relatório final no Slack |
| `criar_boleto_BB.py` | API de Cobranças do Banco do Brasil (OAuth2, convênio por empresa) |
| `criar_boleto_SANTANDER.py` + `boleto_pdf_santander.py` | API do Santander com certificado e-CNPJ (mTLS); como o banco não devolve PDF, o boleto é desenhado com ReportLab, validando código de barras, DV e linha digitável antes |
| `criar_boleto_ABC.py` | API do Banco ABC, com adaptador TLS para a renegociação legada que o servidor do banco exige |
| `criar_boleto_SAFRA.py`, `criar_boleto_SOFISA.py` | Geração de arquivos CNAB (Safra e Sofisa), com cálculo de DV do nosso número dentro da faixa liberada pelo banco |
| `slack_trigger_safra.py`, `slack_trigger_sofisa.py` | Gatilhos via Slack Socket Mode: escrever "Gerar Safra" ou "Gerar Sofisa" no canal dispara a geração, com trava contra execução dupla |
| `slack_zip_boletos.py` | Envia os PDFs dos boletos compactados para o Slack |
| `ajustar_parcelas_clientes.py` | Compara as parcelas do SAP com as duplicatas do XML da NF no Pipefy e ajusta por serviço SOAP |
| `dashboard_2.py` | Painel Flask da carteira: aging, vencimentos, concentração e limite por banco, roteamento de títulos para os bancos de desconto |

## Contas a Pagar (DDA e remessas)

| Arquivo | Função |
|---|---|
| `app.py` | Interface Streamlit: upload da remessa gerada pelo SAP, revisão das exceções e download do arquivo corrigido |
| `processador_BOLETO.py` | Casa boletos DDA com os títulos do SAP e injeta os dados no arquivo |
| `processador_TED.py` | Corrige banco, agência, conta e dígitos dos favorecidos no layout CNAB 240 |
| `processador_PIX.py` | Monta os segmentos de pagamento PIX com tipo e chave por favorecido |
| `gerar_exemplo.py` | Remessa CNAB 240 e retorno do DDA fictícios do modo demonstração (com dígitos verificadores válidos) |

Sem `SAP_PASS` configurado (ou no navegador), o app abre no modo demonstração: a consulta ao DDA do SAP é trocada pelos boletos de `gerar_exemplo.py`, e todo o resto do processamento é o mesmo. `tools/gerar_demo_carteira.py` gera a página do painel da carteira.

## Destaques técnicos

- Cinco integrações bancárias diferentes (REST com OAuth2, mTLS com certificado A1 e CNAB posicional) atrás de uma rotina única, escolhida pelo house bank do título.
- Idempotência: cada título processado é registrado e nunca gera boleto duplicado, o que permite rodar a cada 30 minutos.
- Falha visível de propósito: house bank sem parametrização vira erro no relatório em vez de boleto errado em silêncio.
- Agendamento por systemd timer e serviços em Socket Mode no Slack, sem servidor público.

## Como executar

```bash
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp cobranca/parametros_exemplo.txt cobranca/parametros_prod.txt   # preencha
cd cobranca && python processador_bb_prod.py                       # emite boletos
streamlit run remessas/app.py                                      # remessas do Contas a Pagar
# credenciais das remessas: SAP_DDA_URL, SAP_USER e SAP_PASS em .streamlit/secrets.toml ou no ambiente
```

Requer credenciais de produção ou homologação de cada banco e um usuário de comunicação no SAP.

## Tecnologias

Python 3 · requests · pandas · lxml · Flask · Streamlit · ReportLab · Slack Bolt (Socket Mode) · stlite/Pyodide · SAP S/4HANA (OData, SOAP) · APIs de cobrança BB, Santander e ABC · CNAB 240/400 · systemd

## Autor

João Victor Guidi de Lima · [LinkedIn](https://www.linkedin.com/in/joão-lima-8b512) · [GitHub](https://github.com/Joao-2003)
