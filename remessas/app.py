import streamlit as st
import datetime
import os
import sys
import re
import io
import pandas as pd

from processador_BOLETO import (
    processar_remessa_sap
)

from processador_TED import (
    processar_remessa_ted,
    extrair_excecoes_do_arquivo_original,
    montar_entrada_depara,
    normalizar_cnpj,
)

from processador_PIX import (
    processar_remessa_pix,
    montar_entrada_pix,
    normalizar_cnpj, 
)


# ============================================================
# CONFIGURAÇÃO
# ============================================================

st.set_page_config(
    page_title="Injeção DDA SAP",
    page_icon="🏦",
    layout="wide"
)

st.markdown(
    """
    <style>
        .block-container {
            padding-top: 2rem;
            padding-left: 2rem;
            padding-right: 2rem;
            max-width: 100%;
        }
    </style>
    """,
    unsafe_allow_html=True
)

st.title("Injeção Automática de DDA")

st.caption(
    "Lê o arquivo de remessa do SAP, processa os DDAs automaticamente "
    "e permite gerar TED/PIX para os pagamentos sem DDA."
)

st.divider()


# ============================================================
# CONFIGURAÇÕES SAP
#
# Credenciais fora do código: .streamlit/secrets.toml
# (SAP_DDA_URL, SAP_USER, SAP_PASS) ou variáveis de ambiente
# com os mesmos nomes. Sem SAP_PASS, o app abre no modo
# demonstração (DDA e remessa fictícios, sem acesso ao SAP).
# ============================================================

def _config(nome, padrao=""):

    valor = os.environ.get(nome)

    if valor:
        return valor

    try:
        return st.secrets.get(nome, padrao)
    except Exception:
        return padrao


URL_API = _config(
    "SAP_DDA_URL",
    "https://myXXXXXX-api.s4hana.cloud.sap/"
    "sap/opu/odata/sap/YY1_DDA_CDS/YY1_DDA"
)

SAP_USER = _config("SAP_USER")

SAP_PASS = _config("SAP_PASS")

MODO_DEMO = sys.platform == "emscripten" or not SAP_PASS

if MODO_DEMO:

    import processador_BOLETO
    from gerar_exemplo import gerar as gerar_exemplo

    if "demo_arquivos" not in st.session_state:
        st.session_state.demo_arquivos = gerar_exemplo()

    def _dda_demo(url_api, user, password):
        # mesma interpretação do retorno da API YY1_DDA, com boletos fictícios
        return processador_BOLETO.interpretar_dda(st.session_state.demo_arquivos[1])

    processador_BOLETO.buscar_dados_dda_api = _dda_demo

    SAP_USER, SAP_PASS = "demo", "demo"

    st.info(
        "Modo demonstração: a remessa do SAP e os boletos do DDA são "
        "fictícios e gerados com a data de hoje. O processamento "
        "(casamento DDA, segmentos J/J-52, TED e PIX, arquivo final) "
        "é o mesmo do app em produção."
    )


# ============================================================
# SESSION STATE
# ============================================================

defaults = {

    # --------------------------------------------------------
    # PROCESSAMENTO
    # --------------------------------------------------------

    "processado": False,

    # --------------------------------------------------------
    # RESULTADO DDA
    # --------------------------------------------------------

    "rem_pronta": None,
    "log": None,
    "excel_bytes": None,

    # --------------------------------------------------------
    # EXCEÇÕES
    # --------------------------------------------------------

    "excecoes": [],
    "df_excecoes": None,
    "df_excecoes_base": None,

    # --------------------------------------------------------
    # ESTRUTURA ORIGINAL CNAB
    # --------------------------------------------------------

    "header_arquivo": "",
    "header_lote": "",
    "trailer_lote": "",
    "trailer_arquivo_original": "",

    # --------------------------------------------------------
    # PROCESSADORES
    # --------------------------------------------------------

    "arquivo_boleto_processado": None,
    "arquivo_ted_processado": None,
    "arquivo_pix_processado": None,

    # --------------------------------------------------------
    # LOTES
    # --------------------------------------------------------

    "lote_boleto": None,
    "lote_ted": None,
    "lote_pix": None,

    # --------------------------------------------------------
    # LOGS
    # --------------------------------------------------------

    "log_ted": None,
    "log_pix": None,

    # --------------------------------------------------------
    # EXCEL
    # --------------------------------------------------------

    "excel_ted": None,
    "excel_pix": None,

    # --------------------------------------------------------
    # ARQUIVO FINAL
    # --------------------------------------------------------

    "arquivo_final": None,
    "total_lotes_final": 0,
    "total_registros_final": 0,

    # --------------------------------------------------------
    # CONTROLE DO UPLOAD
    # --------------------------------------------------------

    "nome_arquivo_anterior": None,
}


for key, value in defaults.items():

    if key not in st.session_state:
        st.session_state[key] = value


# ============================================================
# LOG
# ============================================================

def extrair_boletos_do_lote(lote):
    """Extrai os dados de cada boleto diretamente do lote CNAB 240 de Boletos."""
    boletos = []
    if not lote:
        return boletos

    cnpj_por_favorecido = {}
    
    # 1. Mapeia CNPJs se houver Segmento J-52
    for linha in lote:
        if len(linha) >= 240 and linha[7:8] == "3":
            if linha[13:14] == "J" and linha[17:19] == "52":
                num_doc = linha[20:35].strip().lstrip("0")
                nome_fav = linha[35:75].strip()
                if nome_fav and num_doc:
                    cnpj_por_favorecido[nome_fav] = num_doc

    # 2. Extrai boletos dos Segmentos J ou A
    for linha in lote:
        if len(linha) < 240 or linha[7:8] != "3":
            continue
        
        seg = linha[13:14]
        
        if seg == "J" and linha[17:19] != "52":
            favorecido = linha[91:121].strip()
            venc_raw = linha[144:152].strip()
            val_raw = linha[152:167].strip()
            
            try:
                valor = int(val_raw) / 100.0 if val_raw else 0.0
            except Exception:
                valor = 0.0
            
            vencimento = f"{venc_raw[:2]}/{venc_raw[2:4]}/{venc_raw[4:]}" if len(venc_raw) == 8 else venc_raw
            cnpj = cnpj_por_favorecido.get(favorecido, "DDA Processado")
            
            boletos.append({
                "CNPJ/Fornecedor": cnpj,
                "Nome do Favorecido": favorecido,
                "Valor Original (R$)": valor,
                "Vencimento SAP": vencimento,
                "Tipo Lançamento": "DDA",
                "Status": "Aprovado / Processado",
                "Motivo da Ocorrência": "Boleto DDA localizado e vinculado com sucesso"
            })
            
        elif seg == "A":
            favorecido = linha[43:73].strip()
            venc_raw = linha[93:101].strip()
            val_raw = linha[119:134].strip()
            
            try:
                valor = int(val_raw) / 100.0 if val_raw else 0.0
            except Exception:
                valor = 0.0
                
            vencimento = f"{venc_raw[:2]}/{venc_raw[2:4]}/{venc_raw[4:]}" if len(venc_raw) == 8 else venc_raw
            
            boletos.append({
                "CNPJ/Fornecedor": "DDA Processado",
                "Nome do Favorecido": favorecido,
                "Valor Original (R$)": valor,
                "Vencimento SAP": vencimento,
                "Tipo Lançamento": "DDA",
                "Status": "Aprovado / Processado",
                "Motivo da Ocorrência": "Boleto DDA localizado e vinculado com sucesso"
            })
            
    return boletos


def gerar_relatorio_corporativo_unificado():
    registros = []

    # --------------------------------------------------------
    # 1. PROCESSAMENTOS VIA BOLETO / DDA
    #
    # Lidos do LOG do processamento (st.session_state.log),
    # não da releitura do CNAB final: o Segmento J final
    # sobrescreve o valor original pelo valor corrigido da API,
    # então reler o arquivo não permite calcular o Delta.
    # --------------------------------------------------------
    log_processamento = st.session_state.get("log") or {}

    aprovados_info = log_processamento.get("aprovados_info", [])

    for item in aprovados_info:

        valor_original = item.get("valor_original", 0.0)
        valor_corrigido = item.get("valor_corrigido", 0.0)

        registros.append({
            "CNPJ/Fornecedor": item.get("cnpj_beneficiario", ""),
            "Nome do Favorecido": item.get("nome_favorecido", ""),
            "Valor Original (R$)": valor_original,
            "Valor Corrigido (R$)": valor_corrigido,
            "Delta (R$)": item.get("delta", round(valor_corrigido - valor_original, 2)),
            "Vencimento SAP": item.get("vencimento", ""),
            "Tipo Lançamento": "DDA",
            "Status": "Aprovado / Processado",
            "Motivo da Ocorrência": "Boleto DDA localizado e vinculado com sucesso"
        })

    # --------------------------------------------------------
    # 2. PROCESSAMENTOS TED E PIX (EXCEÇÕES DE DDA)
    #
    # Não passam por correção de valor via DDA, então
    # Valor Corrigido = Valor Original e Delta = 0.
    # --------------------------------------------------------
    df_exc = st.session_state.get("df_excecoes")

    if df_exc is not None and not df_exc.empty:
        for _, row in df_exc.iterrows():
            enviado = row.get("Enviar", False)
            tipo = row.get("Tipo", "TED")
            valor_original = row.get("Valor", 0.0)

            if enviado:
                status = "Processado"
                motivo = f"Sem DDA no SAP - Redirecionado e processado via {tipo}"
                tipo_lancamento = tipo
            else:
                status = "Pendente / Bloqueado"
                motivo = "Sem DDA no SAP - Pagamento não selecionado para processamento"
                tipo_lancamento = "Pendente"

            registros.append({
                "CNPJ/Fornecedor": row.get("_cnpj", ""),
                "Nome do Favorecido": row.get("Favorecido", ""),
                "Valor Original (R$)": valor_original,
                "Valor Corrigido (R$)": valor_original,
                "Delta (R$)": 0.0,
                "Vencimento SAP": "",
                "Tipo Lançamento": tipo_lancamento,
                "Status": status,
                "Motivo da Ocorrência": motivo
            })

    if not registros:
        return None

    # Monta o DataFrame com o cabeçalho exato solicitado
    df_final = pd.DataFrame(registros)

    colunas_padrao = [
        "CNPJ/Fornecedor",
        "Nome do Favorecido",
        "Valor Original (R$)",
        "Valor Corrigido (R$)",
        "Delta (R$)",
        "Vencimento SAP",
        "Tipo Lançamento",
        "Status",
        "Motivo da Ocorrência"
    ]

    df_final = df_final.reindex(columns=colunas_padrao)

    buffer = io.BytesIO()
    with pd.ExcelWriter(buffer, engine="xlsxwriter") as writer:
        df_final.to_excel(writer, index=False, sheet_name="Resumo Executivo")
    
    return buffer.getvalue()

# ============================================================
# NORMALIZA LINHAS CNAB
# ============================================================

def normalizar_linhas_cnab(dados):

    if dados is None:
        return []

    if isinstance(dados, bytes):

        texto = dados.decode(
            "latin-1"
        )

    else:

        texto = str(dados)

    texto = (
        texto
        .replace("\r\n", "\n")
        .replace("\r", "\n")
    )

    linhas = []

    for linha in texto.split("\n"):

        linha = linha.rstrip(
            "\n\r"
        )

        if not linha:
            continue

        if len(linha) < 240:

            linha = linha.ljust(
                240
            )

        elif len(linha) > 240:

            linha = linha[:240]

        linhas.append(
            linha
        )

    return linhas


# ============================================================
# EXTRAI LOTES
#
# Funciona para:
#
# - arquivo CNAB completo
# - lote isolado
#
# Ignora:
#
# - registro 0
# - registro 9
# ============================================================

def extrair_lotes_de_arquivo(dados):

    linhas = normalizar_linhas_cnab(
        dados
    )

    lotes = []

    lote_atual = None

    for linha in linhas:

        tipo = linha[7:8]

        # ----------------------------------------------------
        # HEADER LOTE
        # ----------------------------------------------------

        if tipo == "1":

            if lote_atual:

                lotes.append(
                    lote_atual
                )

            lote_atual = [
                linha
            ]

        # ----------------------------------------------------
        # DETALHE
        # ----------------------------------------------------

        elif tipo == "3":

            if lote_atual is not None:

                lote_atual.append(
                    linha
                )

        # ----------------------------------------------------
        # TRAILER LOTE
        # ----------------------------------------------------

        elif tipo == "5":

            if lote_atual is not None:

                lote_atual.append(
                    linha
                )

                lotes.append(
                    lote_atual
                )

                lote_atual = None

    # --------------------------------------------------------
    # Caso exista lote sem trailer
    # --------------------------------------------------------

    if lote_atual:

        lotes.append(
            lote_atual
        )

    return lotes


# ============================================================
# CALCULA TOTAL DO LOTE
# ============================================================

def calcular_total_lote(lote):
    if not lote:
        return 0, 0

    linhas = normalizar_linhas_cnab(lote)
    total_registros = len(linhas)
    total_valor = 0

    for linha in linhas:
        if linha[7:8] != "3":
            continue

        segmento = linha[13:14]

        if segmento == "A":
            campo_valor = linha[119:134]
        elif segmento == "J" and linha[17:19] != "52":
            campo_valor = linha[99:114]
        else:
            continue  # B, J-52 etc. não carregam valor do título

        valor_txt = campo_valor.strip()
        try:
            total_valor += int(valor_txt or 0)
        except Exception:
            pass

    return total_registros, total_valor


# ============================================================
# RENÚMERA LOTE
# ============================================================

def renumerar_lote(lote, numero_lote):

    numero_lote = str(numero_lote).zfill(4)

    resultado = []

    for linha in lote:

        linha = linha[:240].ljust(240)

        # Número do lote: posições 4-7
        linha = (
            linha[:3]
            + numero_lote
            + linha[7:]
        )

        resultado.append(linha)

    return resultado


# ============================================================
# OBTÉM HEADER DO ARQUIVO
# ============================================================

def obter_header_arquivo(
    dados
):

    linhas = normalizar_linhas_cnab(
        dados
    )

    for linha in linhas:

        if linha[7:8] == "0":

            return linha

    return ""


# ============================================================
# OBTÉM HEADER DO LOTE
# ============================================================

def obter_header_lote(
    dados
):

    linhas = normalizar_linhas_cnab(
        dados
    )

    for linha in linhas:

        if linha[7:8] == "1":

            return linha

    return ""


# ============================================================
# OBTÉM TRAILER DO LOTE
# ============================================================

def obter_trailer_lote(
    dados
):

    linhas = normalizar_linhas_cnab(
        dados
    )

    for linha in linhas:

        if linha[7:8] == "5":

            return linha

    return ""


# ============================================================
# OBTÉM TRAILER DO ARQUIVO
# ============================================================

def obter_trailer_arquivo_original(
    dados
):

    linhas = normalizar_linhas_cnab(
        dados
    )

    for linha in linhas:

        if linha[7:8] == "9":

            return linha

    return ""


# ============================================================
# MONTA TRAILER FINAL DO ARQUIVO
# ============================================================

def montar_trailer_arquivo(trailer_original,total_lotes,total_registros):

    if trailer_original:
        trailer = (trailer_original[:240].ljust(240))

    else:
        trailer = " " * 240
    
    trailer = (trailer[:3] + "99999" + trailer[8:])

    trailer = ( trailer[:17] + str(total_lotes).zfill(6) + trailer[23:] )

    trailer = (trailer[:23] + str(total_registros).zfill(6)+ trailer[29:])

    return trailer


# ============================================================
# MONTA ARQUIVO FINAL
# ============================================================

def montar_arquivo_final(
    header_arquivo,
    lotes,
    trailer_arquivo_original
):

    linhas_finais = []

    # ========================================================
    # HEADER DO ARQUIVO
    # ========================================================

    if header_arquivo:

        header = (
            header_arquivo[:240]
            .ljust(240)
        )

        linhas_finais.append(header)

    # ========================================================
    # LOTES
    #
    # IMPORTANTE:
    # O trailer 5 de cada lote já foi montado pelo respectivo
    # processador.
    #
    # Aqui NÃO recalculamos quantidade nem valor.
    # Apenas renumeramos o lote.
    # ========================================================

    numero_lote = 1

    for lote in lotes:

        if not lote:
            continue

        lote_final = renumerar_lote(
            lote,
            numero_lote
        )

        linhas_finais.extend(
            lote_final
        )

        numero_lote += 1

    # ========================================================
    # TOTAL DE LOTES
    # ========================================================

    total_lotes = numero_lote - 1

    # ========================================================
    # TOTAL DE REGISTROS DO ARQUIVO
    #
    # linhas_finais já possui:
    #
    #   1 header arquivo
    #   todos os headers de lote
    #   todos os detalhes
    #   todos os trailers de lote
    #
    # Falta somente o trailer 9.
    # ========================================================

    total_registros = (
        len(linhas_finais) + 1
    )

    # ========================================================
    # TRAILER FINAL DO ARQUIVO
    # ========================================================

    trailer_final = montar_trailer_arquivo(
        trailer_arquivo_original,
        total_lotes,
        total_registros
    )

    linhas_finais.append(
        trailer_final
    )

    # ========================================================
    # ARQUIVO FINAL
    # ========================================================

    arquivo_final = (
        "\r\n".join(linhas_finais)
        .encode("latin-1")
    )

    return (
        arquivo_final,
        total_lotes,
        total_registros
    )

# ============================================================
# ATUALIZA ARQUIVO FINAL
# ============================================================

def atualizar_arquivo_final():

    lotes = []

    # --------------------------------------------------------
    # ORDEM OBRIGATÓRIA
    #
    # 1 = BOLETO
    # 2 = TED
    # 3 = PIX
    # --------------------------------------------------------

    if st.session_state.get(
        "lote_boleto"
    ):

        lotes.append(
            st.session_state.lote_boleto
        )

    if st.session_state.get(
        "lote_ted"
    ):

        lotes.append(
            st.session_state.lote_ted
        )

    if st.session_state.get(
        "lote_pix"
    ):

        lotes.append(
            st.session_state.lote_pix
        )

    # --------------------------------------------------------
    # SEM LOTES
    # --------------------------------------------------------

    if not lotes:

        st.session_state.arquivo_final = None

        st.session_state.total_lotes_final = 0

        st.session_state.total_registros_final = 0

        return

    # --------------------------------------------------------
    # MONTA
    # --------------------------------------------------------

    (
        arquivo_final,
        total_lotes,
        total_registros
    ) = montar_arquivo_final(
        st.session_state.header_arquivo,
        lotes,
        st.session_state.trailer_arquivo_original
    )

    # --------------------------------------------------------
    # GUARDA
    # --------------------------------------------------------

    st.session_state.arquivo_final = (
        arquivo_final
    )

    st.session_state.total_lotes_final = (
        total_lotes
    )

    st.session_state.total_registros_final = (
        total_registros
    )


# ============================================================
# UPLOAD
# ============================================================

file_remessa = st.file_uploader(
    "Suba o Arquivo de Pagamento do SAP (.txt / .rem / .ret):",
    type=[
        "txt",
        "rem",
        "ret"
    ]
)

if MODO_DEMO:

    class _RemessaExemplo(io.BytesIO):
        name = "REMESSA_SAP_EXEMPLO.txt"

    col_ex1, col_ex2 = st.columns(2)

    if col_ex1.button("📄 Usar a remessa de exemplo", use_container_width=True):
        st.session_state.usar_exemplo = True

    col_ex2.download_button(
        "⬇️ Baixar a remessa de exemplo (.txt)",
        data=st.session_state.demo_arquivos[0],
        file_name="REMESSA_SAP_EXEMPLO.txt",
        use_container_width=True
    )

    if not file_remessa and st.session_state.get("usar_exemplo"):
        file_remessa = _RemessaExemplo(st.session_state.demo_arquivos[0])
        st.caption("Usando REMESSA_SAP_EXEMPLO.txt: 13 pagamentos a fornecedores fictícios.")


# ============================================================
# LIMPA ESTADO QUANDO TROCAR O ARQUIVO
# ============================================================

if (
    file_remessa
    and
    st.session_state.get(
        "nome_arquivo_anterior"
    )
    != file_remessa.name
):

    st.session_state.processado = False

    st.session_state.nome_arquivo_anterior = (
        file_remessa.name
    )

    # --------------------------------------------------------
    # RESULTADO DDA
    # --------------------------------------------------------

    st.session_state.rem_pronta = None
    st.session_state.log = None
    st.session_state.excel_bytes = None

    # --------------------------------------------------------
    # EXCEÇÕES
    # --------------------------------------------------------

    st.session_state.excecoes = []
    st.session_state.df_excecoes = None
    st.session_state.df_excecoes_base = None

    # --------------------------------------------------------
    # ESTRUTURA ORIGINAL
    # --------------------------------------------------------

    st.session_state.header_arquivo = ""
    st.session_state.header_lote = ""
    st.session_state.trailer_lote = ""
    st.session_state.trailer_arquivo_original = ""

    # --------------------------------------------------------
    # PROCESSADORES
    # --------------------------------------------------------

    st.session_state.arquivo_boleto_processado = None
    st.session_state.arquivo_ted_processado = None
    st.session_state.arquivo_pix_processado = None

    # --------------------------------------------------------
    # LOTES
    # --------------------------------------------------------

    st.session_state.lote_boleto = None
    st.session_state.lote_ted = None
    st.session_state.lote_pix = None

    # --------------------------------------------------------
    # LOGS
    # --------------------------------------------------------

    st.session_state.log_ted = None
    st.session_state.log_pix = None

    # --------------------------------------------------------
    # EXCEL
    # --------------------------------------------------------

    st.session_state.excel_ted = None
    st.session_state.excel_pix = None

    # --------------------------------------------------------
    # ARQUIVO FINAL
    # --------------------------------------------------------

    st.session_state.arquivo_final = None

    st.session_state.total_lotes_final = 0

    st.session_state.total_registros_final = 0


# ============================================================
# PROCESSAMENTO DDA / BOLETO
# ============================================================

if st.button(
    "🔎 Buscar no SAP e Processar DDA",
    type="primary",
    use_container_width=True
):

    if not file_remessa:

        st.warning(
            "Faça o upload do arquivo para começar."
        )

    elif not SAP_PASS:

        st.error(
            "A senha SAP não foi configurada. "
            "Configure SAP_PASS no Streamlit Secrets "
            "ou como variável de ambiente."
        )

    else:

        try:

            # ====================================================
            # PROCESSA DDA
            # ====================================================

            with st.spinner(
                "Conectando ao SAP S/4HANA e processando os DDAs..."
            ):

                (
                    rem_pronta,
                    log,
                    excel_bytes
                ) = processar_remessa_sap(
                    file_remessa,
                    URL_API,
                    SAP_USER,
                    SAP_PASS
                )

            # ====================================================
            # SALVAMENTO AUTOMÁTICO DE AUDITORIA EM SUBPASTA
            #
            # Restaurado da primeira versão do app: grava um
            # resumo textual cumulativo em execucoes_dda.log e
            # guarda cópias físicas do .txt e do .xlsx gerados
            # dentro de historico_remessas/. A aplicação roda em
            # rede (servidor próprio), então os arquivos locais
            # persistem entre execuções.
            # ====================================================

            try:

                now = datetime.datetime.now()

                timestamp_str = now.strftime(
                    "%Y-%m-%d %H:%M:%S"
                )

                timestamp_file = now.strftime(
                    "%Y%m%d_%H%M%S"
                )

                PASTA_HISTORICO = "historico_remessas"

                if not os.path.exists(PASTA_HISTORICO):

                    os.makedirs(PASTA_HISTORICO)

                texto_log = (
                    "==================================================\n"
                )

                texto_log += (
                    f"DATA/HORA EXECUÇÃO: {timestamp_str}\n"
                )

                texto_log += (
                    f"ARQUIVO ORIGEM SAP: {file_remessa.name}\n"
                )

                texto_log += (
                    "--------------------------------------------------\n"
                )

                texto_log += (
                    f"Conexão OK! {log['mapa_tamanho']} boletos lidos "
                    "da API DDA.\n"
                )

                texto_log += (
                    "Faturas no arquivo transformadas e validadas "
                    f"com sucesso: {log['aprovados']}\n"
                )

                if log.get('bloqueados', 0) > 0:

                    texto_log += (
                        f"🚨 PAGAMENTOS BLOQUEADOS ({log['bloqueados']}): "
                        "Os títulos abaixo não possuem DDA "
                        "correspondente na API e foram expurgados "
                        "por segurança:\n"
                    )

                    for info in log.get('faturas_bloqueadas_info', []):

                        texto_log += f"  ❌ {info}\n"

                if log.get('esquecidas_hoje', 0) > 0:

                    texto_log += (
                        "⚠️ AUDITORIA DE CAIXA: O SAP identificou "
                        f"{log['esquecidas_hoje']} faturas vencendo "
                        "HOJE na API que não foram incluídas nesta "
                        "remessa!\n"
                    )

                texto_log += (
                    "==================================================\n\n"
                )

                # 1. Grava/acumula no arquivo .log histórico
                #    (mantido na raiz para acesso rápido)
                with open(
                    "execucoes_dda.log",
                    "a",
                    encoding="utf-8"
                ) as f_log:

                    f_log.write(texto_log)

                # 2. Salva a cópia física do lote de boleto gerado
                #    dentro da subpasta historico_remessas
                if rem_pronta:

                    caminho_txt = os.path.join(
                        PASTA_HISTORICO,
                        f"REMESSA_DDA_GERADA_{timestamp_file}.txt"
                    )

                    with open(caminho_txt, "wb") as f_txt:

                        f_txt.write(rem_pronta)

                # 3. Salva a cópia física do relatório de
                #    ocorrências bloqueadas (.xlsx) dentro da
                #    subpasta historico_remessas
                if excel_bytes:

                    caminho_xlsx = os.path.join(
                        PASTA_HISTORICO,
                        f"Ocorrencias_Bloqueadas_{timestamp_file}.xlsx"
                    )

                    with open(caminho_xlsx, "wb") as f_xlsx:

                        f_xlsx.write(excel_bytes)

            except Exception as file_err:

                st.warning(
                    "Aviso: Processamento concluído, mas houve uma "
                    "falha ao gravar os arquivos de auditoria: "
                    f"{file_err}"
                )

            # ====================================================
            # LINHAS ORIGINAIS
            # ====================================================

            linhas_originais = (
                normalizar_linhas_cnab(
                    file_remessa.getvalue()
                )
            )

            # ====================================================
            # HEADER ARQUIVO
            # ====================================================

            header_arquivo = (
                obter_header_arquivo(
                    file_remessa.getvalue()
                )
            )

            # ====================================================
            # HEADER LOTE
            # ====================================================

            header_lote = (
                obter_header_lote(
                    file_remessa.getvalue()
                )
            )

            # ====================================================
            # TRAILER LOTE
            # ====================================================

            trailer_lote = (
                obter_trailer_lote(
                    file_remessa.getvalue()
                )
            )

            # ====================================================
            # TRAILER ARQUIVO
            # ====================================================

            trailer_arquivo_original = (
                obter_trailer_arquivo_original(
                    file_remessa.getvalue()
                )
            )

            # ====================================================
            # EXCEÇÕES
            # ====================================================

            excecoes = []

            cnpjs_bloqueados = log.get(
                "cnpjs_bloqueados",
                []
            )

            if cnpjs_bloqueados:

                # ------------------------------------------------
                # EXTRAI EXCEÇÕES DO ORIGINAL
                #
                # RETORNO DEFINITIVO = 5 VALORES
                # ------------------------------------------------

                (
                    excecoes,
                    header_extraido,
                    header_lote_extraido,
                    trailer_lote_extraido,
                    trailer_arquivo_extraido
                ) = (
                    extrair_excecoes_do_arquivo_original(
                        file_remessa,
                        cnpjs_bloqueados
                    )
                )

                # --------------------------------------------
                # HEADER ARQUIVO
                # --------------------------------------------

                if header_extraido:

                    header_arquivo = (
                        header_extraido
                    )

                # --------------------------------------------
                # HEADER LOTE
                # --------------------------------------------

                if header_lote_extraido:

                    header_lote = (
                        header_lote_extraido
                    )

                # --------------------------------------------
                # TRAILER LOTE
                # --------------------------------------------

                if trailer_lote_extraido:

                    trailer_lote = (
                        trailer_lote_extraido
                    )

                # --------------------------------------------
                # TRAILER ARQUIVO
                # --------------------------------------------

                if trailer_arquivo_extraido:

                    trailer_arquivo_original = (
                        trailer_arquivo_extraido
                    )

            # ====================================================
            # GUARDA RESULTADOS
            # ====================================================

            st.session_state.rem_pronta = (
                rem_pronta
            )

            st.session_state.log = (
                log
            )

            st.session_state.excel_bytes = (
                excel_bytes
            )

            st.session_state.excecoes = (
                excecoes
            )

            st.session_state.df_excecoes = None

            st.session_state.df_excecoes_base = None

            st.session_state.header_arquivo = (
                header_arquivo
            )

            st.session_state.header_lote = (
                header_lote
            )

            st.session_state.trailer_lote = (
                trailer_lote
            )

            st.session_state.trailer_arquivo_original = (
                trailer_arquivo_original
            )

            # ====================================================
            # EXTRAI LOTE BOLETO
            # ====================================================

            st.session_state.arquivo_boleto_processado = (
                rem_pronta
            )

            lotes_boleto = (
                extrair_lotes_de_arquivo(
                    rem_pronta
                )
                if rem_pronta
                else []
            )

            # ====================================================
            # BOLETO
            # ====================================================

            if len(lotes_boleto) > 1:

                st.warning(
                    "O processador de boleto retornou "
                    f"{len(lotes_boleto)} lotes. "
                    "Será utilizado o primeiro lote."
                )

            st.session_state.lote_boleto = (
                lotes_boleto[0]
                if lotes_boleto
                else None
            )

            # ====================================================
            # LIMPA TED / PIX ANTERIORES
            # ====================================================

            st.session_state.arquivo_ted_processado = None
            st.session_state.arquivo_pix_processado = None

            st.session_state.lote_ted = None
            st.session_state.lote_pix = None

            st.session_state.log_ted = None
            st.session_state.log_pix = None

            st.session_state.excel_ted = None
            st.session_state.excel_pix = None

            st.session_state.arquivo_final = None

            st.session_state.total_lotes_final = 0
            st.session_state.total_registros_final = 0

            # ====================================================
            # PROCESSADO
            # ====================================================

            st.session_state.processado = True

        except Exception as e:

            st.error(
                f"Erro na integração: {e}"
            )

            st.session_state.processado = False


# ============================================================
# RESULTADOS DO DDA
# ============================================================

if st.session_state.processado:

    log = st.session_state.log

    st.success(
        f"Conexão OK! "
        f"{log['mapa_tamanho']} boletos lidos da API DDA."
    )

    st.info(
        f"Faturas processadas automaticamente com sucesso: "
        f"{log['aprovados']}"
    )

    # ========================================================
    # BLOQUEADOS
    # ========================================================

    bloqueados = log.get(
        "bloqueados",
        0
    )

    if bloqueados > 0:

        st.warning(
            f"⚠️ {bloqueados} pagamento(s) não possuem DDA "
            f"e precisam ser direcionados para TED ou PIX."
        )

    # ========================================================
    # AUDITORIA
    # ========================================================

    if log.get(
        "esquecidas_hoje",
        0
    ) > 0:

        st.warning(
            f"⚠️ AUDITORIA DE CAIXA: O SAP identificou "
            f"{log['esquecidas_hoje']} faturas vencendo HOJE "
            f"na API que não foram incluídas nesta remessa!"
        )

    # ========================================================
    # DOWNLOADS
    # ========================================================

    col1, col2 = st.columns(2)

# ============================================================
# ÁREA DE EXCEÇÕES
# ============================================================

if (
    st.session_state.processado
    and
    st.session_state.log
    and
    st.session_state.log.get(
        "bloqueados",
        0
    ) > 0
):

    st.divider()

    st.header(
        "💸 Pagamentos sem DDA"
    )

    excecoes = (
        st.session_state.excecoes
    )

    if not excecoes:

        st.error(
            "Não consegui identificar automaticamente os "
            "pagamentos sem DDA no arquivo original."
        )

    else:

        total_excecoes = len(
            excecoes
        )

        st.write(
            f"**{total_excecoes} pagamento(s) sem DDA "
            f"identificado(s).**"
        )

        st.caption(
            "Escolha TED ou PIX para cada pagamento. "
            "Para PIX, informe o tipo e a chave."
        )

        # ====================================================
        # DATAFRAME
        # ====================================================

        if st.session_state.get("df_excecoes_base") is None:

            linhas = []

            for idx, exc in enumerate(
                excecoes
            ):

                cnpj_original = str(
                    exc.get(
                        "cnpj",
                        ""
                    )
                ).strip()

                linhas.append({

                    "Enviar": False,

                    "Tipo": "TED",

                    "Favorecido": exc.get(
                        "nome",
                        ""
                    ),

                    "Valor": (
                        float(
                            exc.get(
                                "valor",
                                0
                            )
                        ) / 100
                    ),

                    "Tipo Pix": "CPF/CNPJ",

                    "Chave Pix": cnpj_original,

                    "Banco": exc.get(
                        "banco",
                        ""
                    ),

                    "Agência": exc.get(
                        "agencia",
                        ""
                    ),

                    "DV Ag.": exc.get(
                        "dv_agencia",
                        ""
                    ),

                    "Conta": exc.get(
                        "conta",
                        ""
                    ),

                    "DV Cta.": exc.get(
                        "dv_conta",
                        ""
                    ),

                    "DV Ag/Cta": exc.get(
                        "dv_agencia_conta",
                        ""
                    ),

                    "_idx": idx,

                    "_cnpj": cnpj_original
                })

            st.session_state.df_excecoes_base = (
                pd.DataFrame(
                    linhas
                )
            )

        # ====================================================
        # EDITOR
        # ====================================================

        df_editado = st.data_editor(

            st.session_state.df_excecoes_base,

            key="editor_excecoes",

            hide_index=True,

            use_container_width=True,

            num_rows="fixed",

            column_order=[

                "Enviar",
                "Tipo",
                "Favorecido",
                "Valor",
                "Tipo Pix",
                "Chave Pix",
                "Banco",
                "Agência",
                "DV Ag.",
                "Conta",
                "DV Cta.",
                "DV Ag/Cta"

            ],

            column_config={

                "Enviar":
                    st.column_config.CheckboxColumn(
                        "✓",
                        help=(
                            "Marque o pagamento "
                            "que deve ser gerado."
                        ),
                        default=False,
                        width="small"
                    ),

                "Tipo":
                    st.column_config.SelectboxColumn(
                        "ENVIO",
                        options=[
                            "TED",
                            "PIX"
                        ],
                        width="small"
                    ),

                "Favorecido":
                    st.column_config.TextColumn(
                        "FAVORECIDO",
                        disabled=True,
                        width="large"
                    ),

                "Valor":
                    st.column_config.NumberColumn(
                        "VALOR",
                        disabled=True,
                        format="R$ %.2f",
                        width="small"
                    ),

                "Tipo Pix":
                    st.column_config.SelectboxColumn(
                        "TIPO PIX",
                        options=[
                            "TELEFONE",
                            "EMAIL",
                            "CPF/CNPJ",
                            "ALEATORIA"
                        ],
                        width="medium"
                    ),

                "Chave Pix":
                    st.column_config.TextColumn(
                        "CHAVE PIX",
                        width="large"
                    ),

                "Banco":
                    st.column_config.TextColumn(
                        "BANCO",
                        max_chars=3,
                        width="small"
                    ),

                "Agência":
                    st.column_config.TextColumn(
                        "AGÊNCIA",
                        max_chars=5,
                        width="small"
                    ),

                "DV Ag.":
                    st.column_config.TextColumn(
                        "DV AG.",
                        max_chars=1,
                        width="small"
                    ),

                "Conta":
                    st.column_config.TextColumn(
                        "CONTA",
                        max_chars=12,
                        width="small"
                    ),

                "DV Cta.":
                    st.column_config.TextColumn(
                        "DV CTA.",
                        max_chars=1,
                        width="small"
                    ),

                "DV Ag/Cta":
                    st.column_config.TextColumn(
                        "DV AG/CTA",
                        max_chars=1,
                        width="small"
                    ),

                "_idx": None,

                "_cnpj": None
            }
        )

        # ====================================================
        # GUARDA EDIÇÕES
        # ====================================================

        st.session_state.df_excecoes = (
            df_editado
        )

        # ====================================================
        # SELECIONADOS
        # ====================================================

        selecionados_df = df_editado[
            df_editado["Enviar"] == True
        ]

        quantidade_selecionada = len(
            selecionados_df
        )

        quantidade_ted = len(
            selecionados_df[
                selecionados_df["Tipo"] == "TED"
            ]
        )

        quantidade_pix = len(
            selecionados_df[
                selecionados_df["Tipo"] == "PIX"
            ]
        )

        valor_ted = (

            selecionados_df.loc[
                selecionados_df["Tipo"] == "TED",
                "Valor"
            ].sum()

            if quantidade_ted > 0

            else 0
        )

        valor_pix = (

            selecionados_df.loc[
                selecionados_df["Tipo"] == "PIX",
                "Valor"
            ].sum()

            if quantidade_pix > 0

            else 0
        )

        # ====================================================
        # RESUMO
        # ====================================================

        st.divider()

        col1, col2, col3, col4 = st.columns(4)

        with col1:

            st.metric(
                "Selecionados",
                quantidade_selecionada
            )

        with col2:

            st.metric(
                "TED",
                f"{quantidade_ted} | R$ {valor_ted:,.2f}"
            )

        with col3:

            st.metric(
                "PIX",
                f"{quantidade_pix} | R$ {valor_pix:,.2f}"
            )

        with col4:

            st.metric(
                "Exceções",
                total_excecoes
            )

        # ====================================================
        # BOTÕES
        # ====================================================

        st.divider()

        col_ted, col_pix = st.columns(2)

        # ====================================================
        # TED
        # ====================================================

        with col_ted:

            if st.button(
                "💸 Processar TED",
                type="primary",
                use_container_width=True
            ):

                selecionados_ted = (
                    selecionados_df[
                        selecionados_df["Tipo"] == "TED"
                    ]
                )

                if selecionados_ted.empty:

                    st.warning(
                        "Nenhum pagamento TED foi selecionado."
                    )

                else:

                    erros = []

                    mapa_depara = {}

                    excecoes_confirmadas = []

                    # ========================================
                    # VALIDA TED
                    # ========================================

                    for _, row in (
                        selecionados_ted.iterrows()
                    ):

                        idx = int(
                            row["_idx"]
                        )

                        exc = excecoes[
                            idx
                        ]

                        nome = exc.get(
                            "nome",
                            "Favorecido"
                        )

                        banco = str(
                            row["Banco"]
                        ).strip()

                        agencia = str(
                            row["Agência"]
                        ).strip()

                        dv_agencia = str(
                            row["DV Ag."]
                        ).strip()

                        conta = str(
                            row["Conta"]
                        ).strip()

                        dv_conta = str(
                            row["DV Cta."]
                        ).strip()

                        dv_agencia_conta = str(
                            row["DV Ag/Cta"]
                        ).strip()

                        # ------------------------------------
                        # BANCO
                        # ------------------------------------

                        if not banco:

                            erros.append(
                                f"{nome} — Banco não informado."
                            )

                        elif not banco.isdigit():

                            erros.append(
                                f"{nome} — Banco deve conter somente números."
                            )

                        elif len(banco) != 3:

                            erros.append(
                                f"{nome} — Banco deve possuir 3 dígitos."
                            )

                        # ------------------------------------
                        # AGÊNCIA
                        # ------------------------------------

                        if not agencia:

                            erros.append(
                                f"{nome} — Agência não informada."
                            )

                        # ------------------------------------
                        # CONTA
                        # ------------------------------------

                        if not conta:

                            erros.append(
                                f"{nome} — Conta não informada."
                            )

                        # ------------------------------------
                        # CNPJ
                        # ------------------------------------

                        cnpj_raiz = normalizar_cnpj(
                            exc.get("cnpj", "")
                        )

                        if not cnpj_raiz:

                            erros.append(
                                f"{nome} — CNPJ/CPF não identificado."
                            )

                            continue

                        # ------------------------------------
                        # MAPA
                        # ------------------------------------

                        mapa_depara[cnpj_raiz] = montar_entrada_depara(
                            banco, agencia, dv_agencia,
                            conta, dv_conta, dv_agencia_conta
                        )

                        excecoes_confirmadas.append(
                            exc
                        )

                    # =================================================
                    # ERROS
                    # =================================================

                    if erros:

                        st.error(
                            "A remessa TED não foi gerada."
                        )

                        for erro in erros:

                            st.error(
                                f"❌ {erro}"
                            )

                    else:

                        try:

                            with st.spinner(
                                "Processando TED..."
                            ):

                                # --------------------------------
                                # NOVA ASSINATURA
                                #
                                # O TED recebe somente:
                                #
                                # HEADER LOTE
                                # TRAILER LOTE
                                #
                                # O HEADER ARQUIVO NÃO É MAIS
                                # RESPONSABILIDADE DO PROCESSADOR.
                                # --------------------------------

                                resultado_ted = (
                                    processar_remessa_ted(
                                        excecoes_confirmadas,
                                        mapa_depara,
                                        st.session_state.header_lote,
                                        st.session_state.trailer_lote
                                    )
                                )

                            # =================================================
                            # RESULTADO TED
                            # =================================================

                            if len(
                                resultado_ted
                            ) == 3:

                                (
                                    ted_pronta,
                                    log_ted,
                                    excel_ted
                                ) = resultado_ted

                            else:

                                raise ValueError(
                                    "O processador TED retornou "
                                    f"{len(resultado_ted)} valores. "
                                    "Esperado: 3."
                                )

                            # =================================================
                            # GUARDA
                            # =================================================

                            st.session_state.arquivo_ted_processado = (
                                ted_pronta
                            )

                            st.session_state.log_ted = (
                                log_ted
                            )

                            st.session_state.excel_ted = (
                                excel_ted
                            )

                            # =================================================
                            # EXTRAI LOTE
                            # =================================================

                            lotes_ted = (
                                extrair_lotes_de_arquivo(
                                    ted_pronta
                                )
                                if ted_pronta
                                else []
                            )

                            if len(lotes_ted) > 1:

                                st.warning(
                                    "O processador TED retornou "
                                    f"{len(lotes_ted)} lotes. "
                                    "Será utilizado o primeiro."
                                )

                            st.session_state.lote_ted = (

                                lotes_ted[0]

                                if lotes_ted

                                else None
                            )

                            # =================================================
                            # ATUALIZA ARQUIVO FINAL
                            # =================================================

                            atualizar_arquivo_final()

                            if ted_pronta:

                                st.success(
                                    f"TED processado com sucesso! "
                                    f"{log_ted['corrigidos']} pagamento(s)."
                                )

                        except Exception as erro_ted:

                            st.error(
                                f"Erro ao processar TED: {erro_ted}"
                            )

        # ====================================================
        # PIX
        # ====================================================

        with col_pix:

            if st.button(
                "⚡ Processar PIX",
                type="primary",
                use_container_width=True
            ):

                selecionados_pix = (
                    selecionados_df[
                        selecionados_df["Tipo"] == "PIX"
                    ]
                )

                if selecionados_pix.empty:

                    st.warning(
                        "Nenhum pagamento PIX foi selecionado."
                    )

                else:

                    erros_pix = []

                    mapa_pix = {}

                    excecoes_pix_confirmadas = []

                    # ========================================
                    # PROCESSA CADA PIX
                    # ========================================

                    for _, row in (
                        selecionados_pix.iterrows()
                    ):

                        idx = int(
                            row["_idx"]
                        )

                        exc = excecoes[
                            idx
                        ]

                        nome = exc.get(
                            "nome",
                            "Favorecido"
                        )

                        tipo_pix = str(
                            row["Tipo Pix"]
                        ).strip()

                        chave_pix = str(
                            row["Chave Pix"]
                        ).strip()

                        # ------------------------------------
                        # TIPO
                        # ------------------------------------

                        if not tipo_pix:

                            erros_pix.append(
                                f"{nome} — "
                                "Tipo de chave Pix não informado."
                            )

                            continue

                        # ------------------------------------
                        # CPF/CNPJ
                        # ------------------------------------

                        if (
                            tipo_pix.upper()
                            == "CPF/CNPJ"
                        ):

                            cnpj_original = str(
                                exc.get(
                                    "cnpj",
                                    ""
                                )
                            ).strip()

                            chave_pix = re.sub(
                                r"\D",
                                "",
                                cnpj_original
                            )

                            if not chave_pix:

                                erros_pix.append(
                                    f"{nome} — "
                                    "CPF/CNPJ não encontrado."
                                )

                                continue

                        else:

                            if not chave_pix:

                                erros_pix.append(
                                    f"{nome} — "
                                    "Chave Pix não informada."
                                )

                                continue

                        # ------------------------------------
                        # CNPJ RAIZ
                        # ------------------------------------

                        cnpj_raiz = normalizar_cnpj(
                            exc.get("cnpj", "")
                        )

                        if not cnpj_raiz:

                            erros_pix.append(
                                f"{nome} — "
                                "CNPJ/CPF não identificado."
                            )

                            continue

                        # ------------------------------------
                        # MAPA PIX
                        # ------------------------------------

                        try:
                            mapa_pix[cnpj_raiz] = montar_entrada_pix(
                                exc.get("cnpj", ""), tipo_pix, chave_pix
                            )
                        except ValueError as e:
                            erros_pix.append(f"{nome} — {e}")
                            continue

                        mapa_pix[cnpj_raiz]["nome"] = nome
                        mapa_pix[cnpj_raiz]["valor"] = int(exc.get("valor", 0) or 0)

                        excecoes_pix_confirmadas.append(
                            exc
                        )

                    # =================================================
                    # ERROS
                    # =================================================

                    if erros_pix:

                        st.error(
                            "A remessa PIX não foi gerada."
                        )

                        for erro in erros_pix:

                            st.error(
                                f"❌ {erro}"
                            )

                    else:

                        try:

                            with st.spinner(
                                "Processando PIX..."
                            ):

                                # --------------------------------
                                # NOVA ASSINATURA PIX
                                #
                                # Header arquivo NÃO é mais enviado.
                                # --------------------------------

                                resultado_pix = (
                                    processar_remessa_pix(
                                        excecoes_pix_confirmadas,
                                        mapa_pix,
                                        st.session_state.header_lote,
                                        st.session_state.trailer_lote,
                                        tipo_servico="20"
                                    )
                                )

                            # =================================================
                            # RESULTADO PIX
                            # =================================================

                            if len(
                                resultado_pix
                            ) == 3:

                                (
                                    pix_pronto,
                                    log_pix,
                                    excel_pix
                                ) = resultado_pix

                            else:

                                raise ValueError(
                                    "O processador PIX retornou "
                                    f"{len(resultado_pix)} valores. "
                                    "Esperado: 3."
                                )

                            # =================================================
                            # GUARDA
                            # =================================================

                            st.session_state.arquivo_pix_processado = (
                                pix_pronto
                            )

                            st.session_state.log_pix = (
                                log_pix
                            )

                            st.session_state.excel_pix = (
                                excel_pix
                            )

                            # =================================================
                            # EXTRAI LOTE
                            # =================================================

                            lotes_pix = (
                                extrair_lotes_de_arquivo(
                                    pix_pronto
                                )
                                if pix_pronto
                                else []
                            )

                            if len(lotes_pix) > 1:

                                st.warning(
                                    "O processador PIX retornou "
                                    f"{len(lotes_pix)} lotes. "
                                    "Será utilizado o primeiro."
                                )

                            st.session_state.lote_pix = (

                                lotes_pix[0]

                                if lotes_pix

                                else None
                            )

                            # =================================================
                            # ATUALIZA ARQUIVO FINAL
                            # =================================================

                            atualizar_arquivo_final()

                            if pix_pronto:

                                st.success(
                                    f"PIX processado com sucesso! "
                                    f"{log_pix['corrigidos']} pagamento(s)."
                                )

                        except Exception as erro_pix:

                            st.error(
                                f"Erro ao processar PIX: {erro_pix}"
                            )


# ============================================================
# STATUS DOS LOTES
# ============================================================

if st.session_state.processado:

    st.divider()

    st.subheader(
        "📦 Composição da Remessa Final"
    )

    col1, col2, col3 = st.columns(3)

    # ========================================================
    # BOLETO
    # ========================================================

    with col1:

        if st.session_state.lote_boleto:

            qtd, valor = calcular_total_lote(
                st.session_state.lote_boleto
            )

            st.success(
                f"✅ LOTE BOLETO\n\n"
                f"{qtd} registros"
            )

        else:

            st.info(
                "○ LOTE BOLETO não disponível"
            )

    # ========================================================
    # TED
    # ========================================================

    with col2:

        if st.session_state.lote_ted:

            qtd, valor = calcular_total_lote(
                st.session_state.lote_ted
            )

            st.success(
                f"✅ LOTE TED\n\n"
                f"{qtd} registros"
            )

        else:

            st.info(
                "○ LOTE TED aguardando processamento"
            )

    # ========================================================
    # PIX
    # ========================================================

    with col3:

        if st.session_state.lote_pix:

            qtd, valor = calcular_total_lote(
                st.session_state.lote_pix
            )

            st.success(
                f"✅ LOTE PIX\n\n"
                f"{qtd} registros"
            )

        else:

            st.info(
                "○ LOTE PIX aguardando processamento"
            )


# ============================================================
# REMESSA FINAL
# ============================================================

if st.session_state.processado:

    st.divider()

    st.subheader(
        "📄 Remessa Final"
    )

    tem_boleto = bool(
        st.session_state.lote_boleto
    )

    tem_ted = bool(
        st.session_state.lote_ted
    )

    tem_pix = bool(
        st.session_state.lote_pix
    )

    total_tipos = sum(
        [
            tem_boleto,
            tem_ted,
            tem_pix
        ]
    )

    if total_tipos == 0:

        st.warning(
            "Nenhum lote foi gerado ainda."
        )

    else:

        try:

            # ====================================================
            # MONTA NOVAMENTE
            #
            # Isso garante que o arquivo exibido sempre esteja
            # sincronizado com os lotes atuais.
            # ====================================================

            atualizar_arquivo_final()

            total_lotes = (
                st.session_state.total_lotes_final
            )

            total_registros = (
                st.session_state.total_registros_final
            )

            st.info(
                f"Arquivo final: "
                f"**{total_lotes} lote(s)** | "
                f"**{total_registros} registro(s)**"
            )

            # =================================================
            # DETALHAMENTO
            # =================================================

            ordem = []

            numero_lote = 1

            if tem_boleto:

                ordem.append(
                    f"BOLETO → Lote {numero_lote}"
                )

                numero_lote += 1

            if tem_ted:

                ordem.append(
                    f"TED → Lote {numero_lote}"
                )

                numero_lote += 1

            if tem_pix:

                ordem.append(
                    f"PIX → Lote {numero_lote}"
                )

                numero_lote += 1

            for item in ordem:

                st.write(
                    f"• {item}"
                )

            # =================================================
            # DOWNLOAD ÚNICO
            # =================================================

            if st.session_state.arquivo_final:

                clicou_download_final = st.download_button(
                    label="🏦 BAIXAR REMESSA FINAL CNAB 240",

                    data=(
                        st.session_state.arquivo_final
                    ),

                    file_name=(
                        f"REMESSA_FINAL_"
                        f"{datetime.date.today().strftime('%Y%m%d')}.txt"
                    ),

                    mime="text/plain",

                    type="primary",

                    use_container_width=True
                )

                # =============================================
                # SEGUNDA ENTRADA DE AUDITORIA
                #
                # Fecha o ciclo iniciado no processamento do
                # DDA: registra, no momento em que o arquivo
                # final consolidado (Boleto + TED + PIX) é
                # efetivamente baixado, quantos títulos saíram
                # em cada lote e quantos ainda ficaram
                # pendentes sem tratamento.
                # =============================================

                if clicou_download_final:

                    try:

                        now_final = datetime.datetime.now()

                        timestamp_str_final = (
                            now_final.strftime(
                                "%Y-%m-%d %H:%M:%S"
                            )
                        )

                        timestamp_file_final = (
                            now_final.strftime(
                                "%Y%m%d_%H%M%S"
                            )
                        )

                        PASTA_HISTORICO = (
                            "historico_remessas"
                        )

                        if not os.path.exists(
                            PASTA_HISTORICO
                        ):

                            os.makedirs(
                                PASTA_HISTORICO
                            )

                        df_exc_final = (
                            st.session_state.get(
                                "df_excecoes"
                            )
                        )

                        if (
                            df_exc_final is not None
                            and not df_exc_final.empty
                        ):

                            qtd_enviados = int(
                                df_exc_final[
                                    "Enviar"
                                ].sum()
                            )

                            qtd_pendentes = (
                                len(df_exc_final)
                                - qtd_enviados
                            )

                        else:

                            qtd_enviados = 0

                            qtd_pendentes = 0

                        log_atual = (
                            st.session_state.get(
                                "log"
                            )
                            or {}
                        )

                        texto_log_final = (
                            "=================================="
                            "================\n"
                        )

                        texto_log_final += (
                            "FECHAMENTO DO CICLO - ARQUIVO FINAL "
                            "CONSOLIDADO\n"
                        )

                        texto_log_final += (
                            f"DATA/HORA DOWNLOAD: "
                            f"{timestamp_str_final}\n"
                        )

                        texto_log_final += (
                            "ARQUIVO ORIGEM SAP: "
                            f"{st.session_state.get('nome_arquivo_anterior', '')}\n"
                        )

                        texto_log_final += (
                            "----------------------------------"
                            "----------------\n"
                        )

                        texto_log_final += (
                            f"Composição final: {total_lotes} "
                            f"lote(s) | {total_registros} "
                            "registro(s)\n"
                        )

                        texto_log_final += (
                            f"  • BOLETO (DDA aprovado): "
                            f"{log_atual.get('aprovados', 0)} "
                            "título(s)\n"
                        )

                        texto_log_final += (
                            f"  • Redirecionados p/ TED/PIX: "
                            f"{qtd_enviados} título(s)\n"
                        )

                        if qtd_pendentes > 0:

                            texto_log_final += (
                                "⚠️ Ainda ficaram "
                                f"{qtd_pendentes} título(s) sem "
                                "DDA e SEM tratamento via TED/PIX "
                                "(não marcados para envio) — NÃO "
                                "foram incluídos no arquivo final!\n"
                            )

                        texto_log_final += (
                            "=================================="
                            "================\n\n"
                        )

                        with open(
                            "execucoes_dda.log",
                            "a",
                            encoding="utf-8"
                        ) as f_log_final:

                            f_log_final.write(
                                texto_log_final
                            )

                        caminho_txt_final = (
                            os.path.join(
                                PASTA_HISTORICO,
                                "REMESSA_FINAL_CONSOLIDADA_"
                                f"{timestamp_file_final}.txt"
                            )
                        )

                        with open(
                            caminho_txt_final,
                            "wb"
                        ) as f_txt_final:

                            f_txt_final.write(
                                st.session_state.arquivo_final
                            )

                    except Exception as erro_log_final:

                        st.warning(
                            "Aviso: download concluído, mas "
                            "houve falha ao gravar a auditoria "
                            f"final: {erro_log_final}"
                        )

        except Exception as erro_final:

            st.error(
                f"Erro ao montar a remessa final: "
                f"{erro_final}"
            )

# ============================================================
# RELATÓRIO CORPORATIVO FINAL
# ============================================================

if st.session_state.get("arquivo_final"):
    st.divider()
    st.subheader("📊 Relatório Corporativo Consolidado")
    
    relatorio_excel = gerar_relatorio_corporativo_unificado()
    
    if relatorio_excel:
        st.download_button(
            label="📊 Baixar Resumo Corporativo Processamento (.xlsx)",
            data=relatorio_excel,
            file_name=f"Resumo_Corporativo_Pagamentos_{datetime.date.today().strftime('%Y%m%d')}.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            type="primary",
            use_container_width=True
        )