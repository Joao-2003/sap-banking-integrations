"""
============================================================
GATILHO SLACK -> CNAB SOFISA
============================================================

Mesma lógica do slack_trigger_safra.py, apontando para
criar_boleto_SOFISA.py e escutando o gatilho "Gerar Sofisa".

Requisitos:
  pip install slack_bolt

Variáveis de ambiente necessárias:
  SLACK_BOT_TOKEN    -> xoxb-...
  SLACK_APP_TOKEN    -> xapp-...
  SOFISA_CHANNEL_ID  -> ID do canal (ex: C0BQHJEDW1Z)

Como rodar (via systemd, ver slack-trigger-sofisa.service):
  python slack_trigger_sofisa.py
============================================================
"""

import os
import sys
import subprocess
import threading
import logging
import fcntl

from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

# ============================================================
# CONFIGURAÇÃO
# ============================================================

SLACK_BOT_TOKEN = os.environ["SLACK_BOT_TOKEN"]
SLACK_APP_TOKEN = os.environ["SLACK_APP_TOKEN"]
CANAL_PERMITIDO = os.environ["SOFISA_CHANNEL_ID"]

# Texto que dispara o gatilho (normalizado: minúsculo e sem espaços nas pontas)
GATILHO_TEXTO = "gerar sofisa"

# Caminho do script a ser executado.
CAMINHO_SCRIPT = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "criar_boleto_SOFISA.py"
)

# Pasta de trabalho onde o script deve rodar
# (precisa ser a pasta onde estão parametros_prod.txt,
# titulos_processados_sofisa.txt, sequencial_sofisa.txt, etc.)
PASTA_TRABALHO = os.path.dirname(CAMINHO_SCRIPT)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("slack_trigger_sofisa")

app = App(token=SLACK_BOT_TOKEN)

# ============================================================
# CONTROLE DE EXECUÇÃO SIMULTÂNEA
# ============================================================
#
# IMPORTANTE: criar_boleto_SOFISA.py e criar_boleto_SAFRA.py leem e
# escrevem no MESMO arquivo "titulos_processados.csv". Por isso a trava
# não pode ser só dentro deste processo (threading.Lock) — ela precisa
# valer entre os dois serviços (slack-trigger-safra e slack-trigger-sofisa),
# que rodam como processos systemd separados. Usamos um lock de arquivo
# (fcntl.flock), no MESMO caminho usado pelo slack_trigger_safra.py.

LOCK_FILE_PATH = os.path.join(PASTA_TRABALHO, ".cnab_execucao.lock")


def rodar_script_sofisa(say):
    """
    Executa o criar_boleto_SOFISA.py em processo separado.
    Bloqueia novas execuções (deste OU do Safra) enquanto esta estiver
    rodando, via lock de arquivo compartilhado.
    """

    lock_fd = open(LOCK_FILE_PATH, "w")
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        say(
            "⚠️ Já existe uma geração de CNAB em andamento (Safra ou Sofisa "
            "compartilham o mesmo arquivo de controle). Aguarde terminar."
        )
        lock_fd.close()
        return

    try:
        logger.info("Iniciando execução de %s", CAMINHO_SCRIPT)

        resultado = subprocess.run(
            [sys.executable, CAMINHO_SCRIPT],
            cwd=PASTA_TRABALHO,
            capture_output=True,
            text=True,
            timeout=1800,  # 30 min de limite de segurança
        )

        logger.info("STDOUT:\n%s", resultado.stdout)

        if resultado.returncode != 0:
            logger.error("STDERR:\n%s", resultado.stderr)
            say(
                "❌ Ocorreu um erro ao gerar o CNAB SOFISA "
                f"(código de saída {resultado.returncode}). "
                "Verifique o log do servidor para detalhes."
            )

    except subprocess.TimeoutExpired:
        logger.exception("Timeout ao executar o script SOFISA")
        say("❌ A geração do CNAB SOFISA excedeu o tempo limite (30 min) e foi interrompida.")

    except Exception:
        logger.exception("Erro inesperado ao executar o script SOFISA")
        say("❌ Erro inesperado ao tentar gerar o CNAB SOFISA. Verifique o log do servidor.")

    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        lock_fd.close()


# ============================================================
# LISTENER DE MENSAGENS
# ============================================================

@app.event("message")
def lidar_com_mensagem(event, say, logger):

    if event.get("subtype") is not None:
        return
    if event.get("bot_id") is not None:
        return

    canal = event.get("channel")
    texto = (event.get("text") or "").strip().lower()

    if canal != CANAL_PERMITIDO:
        return

    if texto != GATILHO_TEXTO:
        return

    usuario = event.get("user")
    logger.info("Gatilho 'Gerar Sofisa' recebido do usuário %s no canal %s", usuario, canal)

    say("⏳ Iniciando geração do CNAB SOFISA, aguarde...")

    thread = threading.Thread(target=rodar_script_sofisa, args=(say,), daemon=True)
    thread.start()


# ============================================================
# INICIALIZAÇÃO
# ============================================================

if __name__ == "__main__":
    logger.info("Conectando ao Slack via Socket Mode...")
    handler = SocketModeHandler(app, SLACK_APP_TOKEN)
    handler.start()
