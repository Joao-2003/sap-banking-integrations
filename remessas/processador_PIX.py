import re
from datetime import datetime
from io import BytesIO
import pandas as pd


# ============================================================
# NORMALIZAÇÃO
# ============================================================

def normalizar_cnpj(cnpj_str):
    if cnpj_str is None:
        return ""
    limpo = re.sub(r"\D", "", str(cnpj_str))
    return limpo.lstrip("0")


def normalizar_documento(cnpj_str):
    if cnpj_str is None:
        return ""
    return re.sub(r"\D", "", str(cnpj_str))


def normalizar_chave_pix(chave, tipo_pix):
    if chave is None:
        return ""
    chave = str(chave).strip()
    tipo = str(tipo_pix or "").strip().upper()

    if tipo in {"CPF", "CNPJ", "CPF/CNPJ", "CPF / CNPJ", "003"}:
        return re.sub(r"\D", "", chave)

    return chave


# ============================================================
# TIPO DE CHAVE PIX
# ============================================================

def obter_codigo_iniciacao_pix(tipo_pix):
    tipo = str(tipo_pix or "").strip().upper()

    mapa = {
        "001": "001", "TELEFONE": "001", "CELULAR": "001", "PHONE": "001",
        "002": "002", "EMAIL": "002", "E-MAIL": "002",
        "003": "003", "CPF": "003", "CNPJ": "003", "CPF/CNPJ": "003", "CPF / CNPJ": "003",
        "004": "004", "ALEATORIA": "004", "ALEATÓRIA": "004", "RANDOM": "004",
    }

    codigo = mapa.get(tipo)
    if not codigo:
        raise ValueError(f"Tipo de chave Pix inválido: '{tipo_pix}'.")

    return codigo


def montar_entrada_pix(cnpj_favorecido_raw, tipo_pix, chave_pix, txid=""):
    tipo_pix = str(tipo_pix or "").strip()
    codigo_iniciacao = obter_codigo_iniciacao_pix(tipo_pix)  # já valida o tipo
    return {
        "cnpj_favorecido_raw": str(cnpj_favorecido_raw).strip(),
        "tipo_pix": tipo_pix,
        "codigo_iniciacao": codigo_iniciacao,
        "chave_pix": normalizar_chave_pix(chave_pix, tipo_pix),
        "txid": str(txid or "").strip(),
    }


def montar_mapa_pix(df_depara):
    for coluna in ["cnpj_favorecido", "tipo_pix", "chave_pix"]:
        if coluna not in df_depara.columns:
            raise ValueError(f"Coluna obrigatória não encontrada no de-para Pix: {coluna}")

    mapa = {}
    for _, linha in df_depara.iterrows():
        cnpj_raiz = normalizar_cnpj(linha.get("cnpj_favorecido", ""))
        if not cnpj_raiz:
            continue
        txid = str(linha["txid"]).strip() if "txid" in linha and pd.notna(linha["txid"]) else ""
        try:
            mapa[cnpj_raiz] = montar_entrada_pix(
                linha.get("cnpj_favorecido", ""), linha.get("tipo_pix", ""),
                linha.get("chave_pix", ""), txid
            )
        except ValueError:
            continue
    return mapa


def montar_inscricao_pix(cnpj_raw, codigo_iniciacao):
    documento = normalizar_documento(cnpj_raw)

    if not documento:
        return (" ", " " * 14)

    if len(documento) <= 11:
        return ("1", documento.zfill(14)[:14])

    return ("2", documento.rjust(14)[:14])


# ============================================================
# MONTAGEM DE SEGMENTOS CNAB
# ============================================================

def build_header_lote_pix(header_lote_original, num_lote):
    base = header_lote_original if header_lote_original else " " * 240
    header = list(base.ljust(240)[:240])

    header[3:7] = list(str(num_lote).zfill(4)[:4])
    header[11:13] = list("45")  # Forma de Lançamento PIX

    return "".join(header)


def build_segment_a_pix(linha_a_original, num_lote, num_sequencial, dados_corretos):
    base = linha_a_original if linha_a_original else " " * 240
    linha = list(base.ljust(240)[:240])

    linha[3:7] = list(str(num_lote).zfill(4)[:4])
    linha[8:13] = list(str(num_sequencial).zfill(5)[:5])
    linha[17:20] = list("009")  # Câmara Centralizadora PIX

    if dados_corretos:
        if 'banco' in dados_corretos:
            linha[0:3] = list(str(dados_corretos['banco']).zfill(3)[:3])
        if 'agencia' in dados_corretos:
            linha[23:28] = list(str(dados_corretos['agencia']).zfill(5)[:5])
        if 'conta' in dados_corretos:
            linha[35:47] = list(str(dados_corretos['conta']).zfill(12)[:12])

    return "".join(linha)


def build_segment_b_pix(
    linha_b_original,
    codigo_banco,
    num_lote,
    num_sequencial,
    forma_iniciacao,
    tipo_inscricao,
    num_inscricao,
    chave_pix_ou_tipo_conta,
    txid="",
    info_11="",
    ispb=""
):
    base = linha_b_original if linha_b_original else " " * 240
    b = list(base.ljust(240)[:240])

    # Trata caso a forma de iniciação venha '00', vazia ou inválida
    forma_clean = str(forma_iniciacao or "").strip()
    if forma_clean in ("", "0", "00", "000"):
        forma_clean = "003"  # Fallback padrão CPF/CNPJ

    b[0:3] = list(str(codigo_banco).zfill(3)[:3])
    b[3:7] = list(str(num_lote).zfill(4)[:4])
    b[7] = "3"
    b[8:13] = list(str(num_sequencial).zfill(5)[:5])
    b[13] = "B"

    # Forma de Iniciação
    if str(codigo_banco).zfill(3) == "001":
        # Banco do Brasil: 3 dígitos ('001', '002', '003', '004', '005')
        b[14:17] = list(forma_clean.zfill(3)[-3:])
    else:
        # Santander (033) e outros: 2 dígitos ('01' a '05') + 1 espaço reservado na pos. 17
        b[14:16] = list(forma_clean.zfill(2)[-2:])
        b[16] = " "

    b[17] = str(tipo_inscricao)[:1]
    b[18:32] = list(str(num_inscricao).rjust(14, "0")[:14])
    b[32:67] = list(str(txid).ljust(35)[:35])
    b[67:127] = list(str(info_11).ljust(60)[:60])
    b[127:226] = list(str(chave_pix_ou_tipo_conta).ljust(99)[:99])

    if str(codigo_banco).zfill(3) == "001":
        b[226:232] = list("000000")
    else:
        b[226:232] = list(" " * 6)

    if ispb:
        b[232:240] = list(str(ispb).zfill(8)[:8])

    return "".join(b)


# ============================================================
# RESEQUENCIAMENTO E PROCESSAMENTO PRINCIPAL
# ============================================================

def _resequenciar_lote_pix(registros_detalhe, header_lote_original, trailer_lote_original, num_lote=1, tipo_servico="20"):
    lote_str = str(num_lote).zfill(4)

    header = list(str(header_lote_original or "").ljust(240))
    header[3:7] = list(lote_str)
    header[7] = "1"
    header[8] = "C"

    tipo_servico = str(tipo_servico or "20").strip()
    if tipo_servico not in {"20", "30", "98"}:
        raise ValueError("tipo_servico deve ser 20, 30 ou 98.")

    header[9:11] = list(tipo_servico)
    header[11:13] = list("45")  # Forma de Lançamento PIX
    header_final = "".join(header)[:240].ljust(240)

    detalhes_finais = []
    seq = 1
    soma_valores = 0

    for reg in registros_detalhe:
        r = list(str(reg).ljust(240))
        r[3:7] = list(lote_str)
        r[8:13] = list(str(seq).zfill(5))

        linha_final = "".join(r)[:240].ljust(240)
        detalhes_finais.append(linha_final)

        if len(linha_final) > 13 and linha_final[13] == "A":
            try:
                soma_valores += int(linha_final[119:134])
            except ValueError:
                pass

        seq += 1

    qtd_registros_lote = len(detalhes_finais) + 2

    trailer = list(str(trailer_lote_original or "").ljust(240))
    trailer[3:7] = list(lote_str)
    trailer[7] = "5"
    trailer[17:23] = list(str(qtd_registros_lote).zfill(6))
    trailer[23:41] = list(str(soma_valores).zfill(18))

    trailer_final = "".join(trailer)[:240].ljust(240)

    return [header_final, *detalhes_finais, trailer_final]


def processar_remessa_pix(
    excecoes_passo1,
    mapa_pix,
    header_lote_original,
    trailer_lote_original,
    tipo_servico="20"
):
    registros_detalhe = []
    dados_ainda_pendentes = []

    log = {
        "total_excecoes": len(excecoes_passo1),
        "corrigidos": 0,
        "ainda_pendentes": 0,
    }

    seq_registro = 1

    for exc in excecoes_passo1:
        linha_a = str(exc.get("linha_a", "")).ljust(240)
        linha_b = str(exc.get("linha_b", "")).ljust(240)
        cnpj_raw = exc.get("cnpj", "")
        nome = exc.get("nome", "")
        valor = int(exc.get("valor", 0) or 0)

        cnpj_raiz = normalizar_cnpj(cnpj_raw)
        dados_pix = mapa_pix.get(cnpj_raiz)

        if not dados_pix:
            log["ainda_pendentes"] += 1
            dados_ainda_pendentes.append({
                "CNPJ/CPF Favorecido": cnpj_raw,
                "Nome do Favorecido": nome,
                "Valor (R$)": f"{valor / 100:.2f}",
                "Status": "Pendente",
                "Motivo": "Chave Pix não informada no de-para",
            })
            continue

        dados_pix = dict(dados_pix)
        dados_pix["cnpj_favorecido_raw"] = cnpj_raw
        dados_pix["nome"] = nome
        dados_pix["valor"] = valor

        # Monta Segmento A
        linha_a_pix = build_segment_a_pix(
            linha_a_original=linha_a,
            num_lote=1,
            num_sequencial=seq_registro,
            dados_corretos=dados_pix
        )
        seq_registro += 1

        # Monta Segmento B extraindo os dados mapeados pelo app_2_2
        tipo_insc, num_insc = montar_inscricao_pix(
            dados_pix.get("cnpj_favorecido_raw", ""),
            dados_pix.get("codigo_iniciacao", "003")
        )

        codigo_banco = linha_b[:3] if linha_b[:3].isdigit() else "001"

        linha_b_pix = build_segment_b_pix(
            linha_b_original=linha_b,
            codigo_banco=codigo_banco,
            num_lote=1,
            num_sequencial=seq_registro,
            forma_iniciacao=dados_pix.get("codigo_iniciacao", "003"),
            tipo_inscricao=tipo_insc,
            num_inscricao=num_insc,
            chave_pix_ou_tipo_conta=dados_pix.get("chave_pix", ""),
            txid=dados_pix.get("txid", "")
        )
        seq_registro += 1

        registros_detalhe.append(linha_a_pix)
        registros_detalhe.append(linha_b_pix)
        log["corrigidos"] += 1

    if not registros_detalhe:
        bytes_pronto = None
    else:
        lote = _resequenciar_lote_pix(
            registros_detalhe,
            header_lote_original,
            trailer_lote_original,
            num_lote=1,
            tipo_servico=tipo_servico
        )
        bytes_pronto = "\r\n".join(lote).encode("latin-1")

    excel_bytes = None
    if dados_ainda_pendentes:
        df = pd.DataFrame(dados_ainda_pendentes)
        output = BytesIO()
        with pd.ExcelWriter(output, engine="xlsxwriter") as writer:
            df.to_excel(writer, index=False, sheet_name="Pendencias")
            ws = writer.sheets["Pendencias"]
            for idx, col in enumerate(df.columns):
                max_len = max(df[col].astype(str).map(len).max(), len(col)) + 2
                ws.set_column(idx, idx, max_len)
        excel_bytes = output.getvalue()

    return (bytes_pronto, log, excel_bytes)