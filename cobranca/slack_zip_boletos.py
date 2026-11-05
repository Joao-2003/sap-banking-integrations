"""
============================================================
ZIP DE BOLETOS NO SLACK
============================================================

Junta os PDFs de boletos gerados numa execução em um .zip e envia
para o canal do Slack (de preferência na thread do relatório).

    from slack_zip_boletos import enviar_zip_slack
    enviar_zip_slack(lista_de_pdfs, SLACK_TOKEN, SLACK_CANAL, thread_ts=ts_do_relatorio)

Usa o fluxo de upload atual do Slack (o files.upload foi descontinuado):
    1. files.getUploadURLExternal  -> URL de upload + id do arquivo
    2. POST do arquivo nessa URL
    3. files.completeUploadExternal -> publica no canal / thread

Requisito no app do Slack: escopo de bot "files:write" (e reinstalar o app).
O bot precisa ser membro do canal (já é, pois posta o relatório).
"""

import json
import os
import zipfile
from datetime import datetime

import requests

SLACK_API = "https://slack.com/api"


def _montar_zip(arquivos, pasta_zip, prefixo):
    os.makedirs(pasta_zip, exist_ok=True)
    caminho_zip = os.path.join(pasta_zip, f"{prefixo}_{datetime.now():%Y-%m-%d_%H%M}.zip")

    usados = set()
    with zipfile.ZipFile(caminho_zip, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for arquivo in arquivos:
            nome = os.path.basename(arquivo)
            base, ext = os.path.splitext(nome)
            n = 2
            while nome in usados:  # mesmo nome duas vezes: não sobrescreve dentro do zip
                nome = f"{base}_{n}{ext}"
                n += 1
            usados.add(nome)
            zf.write(arquivo, arcname=nome)

    return caminho_zip, sorted(usados)


def _erro(etapa, resposta):
    try:
        corpo = resposta.json()
    except ValueError:
        corpo = {"error": resposta.text[:200]}
    erro = corpo.get("error", f"HTTP {resposta.status_code}")
    dica = {
        "missing_scope": " (adicione o escopo files:write ao app do Slack e reinstale o app)",
        "not_in_channel": " (o bot precisa ser membro do canal)",
        "channel_not_found": " (confira o SLACK_CANAL)",
        "invalid_auth": " (confira o SLACK_TOKEN)",
    }.get(erro, "")
    return f"{etapa}: {erro}{dica}"


def enviar_zip_slack(arquivos, token, canal, thread_ts=None, pasta_zip="boletos_santander_zip",
                     prefixo="boletos_santander", comentario=None, apagar_zip_apos_envio=True):
    """
    Retorna {"ok": bool, "arquivo": caminho_do_zip, "qtd": n, "erro": texto}.
    Nunca lança exceção: falha no Slack não pode derrubar o processador.
    """
    arquivos = [a for a in (arquivos or []) if a and os.path.isfile(a)]

    print()
    print("=" * 120)
    print("SLACK - ZIP DOS BOLETOS")
    print("=" * 120)

    if not arquivos:
        print("Nenhum PDF para enviar.")
        return {"ok": True, "arquivo": None, "qtd": 0, "erro": None}

    try:
        caminho_zip, nomes = _montar_zip(arquivos, pasta_zip, prefixo)
        nome_zip = os.path.basename(caminho_zip)
        tamanho = os.path.getsize(caminho_zip)
        print(f"Zip: {caminho_zip} ({len(nomes)} PDF(s), {tamanho / 1024:.0f} KB)")

        cabecalho = {"Authorization": f"Bearer {token}"}

        # 1. pedir URL de upload
        r1 = requests.post(f"{SLACK_API}/files.getUploadURLExternal", headers=cabecalho,
                           data={"filename": nome_zip, "length": tamanho}, timeout=30)
        d1 = r1.json() if r1.ok else {}
        if not d1.get("ok"):
            raise RuntimeError(_erro("files.getUploadURLExternal", r1))

        # 2. enviar o arquivo
        with open(caminho_zip, "rb") as f:
            r2 = requests.post(d1["upload_url"], files={"file": (nome_zip, f, "application/zip")}, timeout=120)
        if r2.status_code != 200:
            raise RuntimeError(f"upload do arquivo: HTTP {r2.status_code}")

        # 3. publicar no canal / thread
        texto = comentario or f"Boletos Santander desta execução: {len(nomes)} PDF(s)."
        dados = {
            "files": json.dumps([{"id": d1["file_id"], "title": nome_zip}]),
            "channel_id": canal,
            "initial_comment": texto,
        }
        if thread_ts:
            dados["thread_ts"] = thread_ts
        r3 = requests.post(f"{SLACK_API}/files.completeUploadExternal", headers=cabecalho, data=dados, timeout=30)
        d3 = r3.json() if r3.ok else {}
        if not d3.get("ok"):
            raise RuntimeError(_erro("files.completeUploadExternal", r3))

        print(f"Zip enviado ao Slack com sucesso{' na thread do relatório' if thread_ts else ''}.")
        for nome in nomes:
            print(f"  - {nome}")

        if apagar_zip_apos_envio:
            try:
                os.remove(caminho_zip)
            except OSError:
                pass

        return {"ok": True, "arquivo": caminho_zip, "qtd": len(nomes), "erro": None}

    except Exception as erro:
        print(f"AVISO: zip dos boletos NÃO enviado ao Slack: {erro}")
        return {"ok": False, "arquivo": None, "qtd": len(arquivos), "erro": str(erro)}
