import re
from io import BytesIO
import pandas as pd


# ============================================================
# PARSE / NORMALIZAÇÃO DE DADOS BANCÁRIOS (DE-PARA)
# ============================================================

def parse_agencia_conta(valor_str):
    """
    Recebe uma string no formato 'NUMERO-DV'
    Exemplo:
        2963-7
        21895-2

    Retorna:
        numero, dv

    Também aceita:
        2963
        21895
    """

    if valor_str is None:
        return "", ""

    bruto = str(valor_str).strip()

    if not bruto:
        return "", ""

    if "-" in bruto:
        numero, dv = bruto.rsplit("-", 1)
    else:
        numero, dv = bruto, ""

    numero = re.sub(r'\D', '', numero)
    dv = re.sub(r'\D', '', dv)

    return numero, dv


# ============================================================
# NORMALIZAÇÃO CNPJ
# ============================================================

def normalizar_cnpj(cnpj_str):
    """
    Remove tudo que não for dígito e zeros à esquerda.
    """

    limpo = re.sub(
        r'\D',
        '',
        str(cnpj_str)
    )

    return limpo.lstrip('0')


# ============================================================
# MONTA MAPA DE-PARA
# ============================================================

def montar_entrada_depara(banco, agencia, dv_agencia, conta, dv_conta, dv_agencia_conta=""):
    """Normaliza um registro de dados bancários — usado tanto pelo de-para
    em lote quanto pela entrada manual feita direto na tela."""
    return {
        'banco': re.sub(r'\D', '', str(banco)).zfill(3)[:3],
        'agencia': re.sub(r'\D', '', str(agencia)),
        'dv_agencia': re.sub(r'\D', '', str(dv_agencia)),
        'conta': re.sub(r'\D', '', str(conta)),
        'dv_conta': re.sub(r'\D', '', str(dv_conta)),
        'dv_agencia_conta': re.sub(r'\D', '', str(dv_agencia_conta)),
    }


def montar_mapa_depara(df_depara):
    mapa = {}
    for _, linha in df_depara.iterrows():
        cnpj_raiz = normalizar_cnpj(linha['cnpj_favorecido'])
        if not cnpj_raiz:
            continue

        agencia_num, agencia_dv = parse_agencia_conta(linha['agencia_dv'])
        conta_num, conta_dv = parse_agencia_conta(linha['conta_dv'])
        dv_agencia_conta = linha.get('dv_agencia_conta', '') if 'dv_agencia_conta' in linha else ''

        mapa[cnpj_raiz] = montar_entrada_depara(
            linha['banco'], agencia_num, agencia_dv, conta_num, conta_dv, dv_agencia_conta
        )
    return mapa


# ============================================================
# SEGMENTO A
# ============================================================

def build_segment_a(
    linha_a_original,
    dados_corretos
):
    linha = list(
        linha_a_original.ljust(240)
    )

    # NOVO: Altera o valor das posições 18 a 20 (índices 17:20) para "018"
    linha[17:20] = list("018")

    banco = (
        dados_corretos['banco']
        .zfill(3)[:3]
    )

    agencia = (
        dados_corretos['agencia']
        .zfill(5)[:5]
    )

    dv_agencia = (
        dados_corretos['dv_agencia'][:1]
        if dados_corretos['dv_agencia']
        else ' '
    )

    conta = (
        dados_corretos['conta']
        .zfill(12)[:12]
    )

    dv_conta = (
        dados_corretos['dv_conta'][:1]
        if dados_corretos['dv_conta']
        else ' '
    )

    dv_agencia_conta = (
        dados_corretos['dv_agencia_conta'][:1]
        if dados_corretos['dv_agencia_conta']
        else ' '
    )

    # --------------------------------------------------------
    # ALTERAÇÕES BANCÁRIAS
    # --------------------------------------------------------

    linha[20:23] = list(banco)

    linha[23:28] = list(agencia)

    linha[28] = dv_agencia

    linha[29:41] = list(conta)

    linha[41] = dv_conta

    linha[42] = dv_agencia_conta

    return (
        "".join(linha)
        [:240]
        .ljust(240)
    )


# ============================================================
# SEGMENTO B
# ============================================================

def build_segment_b(
    linha_b_original,
    dados_corretos
):
    """
    Segmento B permanece 100% original.

    Os dados bancários ficam no Segmento A.
    """

    return linha_b_original.ljust(240)


# ============================================================
# RESEQUENCIAMENTO DO LOTE TED
# ============================================================

def _resequenciar_lote(
    registros_detalhe,
    header_lote_original,
    trailer_lote_original,
    num_lote
):
    lote_str = str(num_lote).zfill(4)

    # ========================================================
    # HEADER DO LOTE
    # ========================================================
    header = list(header_lote_original.ljust(240))
    
    # Número do lote
    header[3:7] = list(lote_str)

    # NOVO: Altera o valor das posições 12 a 13 (índices 11:13) para "03"
    header[11:13] = list("03")

    header_final = (
        "".join(header)
        [:240]
        .ljust(240)
    )

    # ========================================================
    # DETALHES
    # ========================================================

    detalhes_finais = []

    seq = 1

    soma_valores = 0

    for reg in registros_detalhe:

        r = list(
            reg.ljust(240)
        )

        # ----------------------------------------------------
        # Número do lote
        # ----------------------------------------------------

        r[3:7] = list(
            lote_str
        )

        # ----------------------------------------------------
        # Número sequencial do registro
        # ----------------------------------------------------

        r[8:13] = list(
            str(seq).zfill(5)
        )

        linha_final = (
            "".join(r)
            [:240]
            .ljust(240)
        )

        detalhes_finais.append(
            linha_final
        )

        # ----------------------------------------------------
        # Soma somente Segmento A
        # ----------------------------------------------------

        if (
            len(reg) > 13
            and reg[13] == 'A'
        ):

            try:

                soma_valores += int(
                    reg[119:134]
                )

            except (ValueError, TypeError):

                pass

        seq += 1

    # ========================================================
    # QUANTIDADE TOTAL DO LOTE
    #
    # Header + detalhes + trailer
    # ========================================================

    qtd_registros_lote = (
        len(detalhes_finais) + 2
    )

    # ========================================================
    # TRAILER DO LOTE
    #
    # Usa o TRAILER ORIGINAL como modelo.
    #
    # Não criamos um trailer "do zero".
    # ========================================================

    trailer = list(
        trailer_lote_original.ljust(240)
    )

    # Número do lote
    trailer[3:7] = list(
        lote_str
    )

    # Quantidade de registros do lote
    trailer[17:23] = list(
        str(
            qtd_registros_lote
        ).zfill(6)
    )

    # Soma dos valores
    trailer[23:41] = list(
        str(
            soma_valores
        ).zfill(18)
    )

    trailer_final = (
        "".join(trailer)
        [:240]
        .ljust(240)
    )

    # ========================================================
    # RETORNO DO LOTE
    # ========================================================

    return (
        [header_final]
        + detalhes_finais
        + [trailer_final]
    )


# ============================================================
# PROCESSAMENTO DA REMESSA TED
# ============================================================

def processar_remessa_ted(
    excecoes_passo1,
    mapa_depara,
    header_lote_original,
    trailer_lote_original
):
    """
    Gera SOMENTE o lote TED.

    Não gera:

        Registro 0 - Header de Arquivo
        Registro 9 - Trailer de Arquivo

    O APP será responsável pela montagem do arquivo final.

    Parâmetros
    ----------

    excecoes_passo1:
        Lista das exceções identificadas no processamento DDA.

    mapa_depara:
        Dicionário de CNPJ -> dados bancários.

    header_lote_original:
        Header original do lote.

    trailer_lote_original:
        Trailer original do lote.

    Retorno:

        bytes_pronto
        log
        excel_bytes
    """

    registros_detalhe_corrigidos = []

    dados_ainda_pendentes = []

    log = {
        'total_excecoes':
            len(excecoes_passo1),

        'corrigidos':
            0,

        'ainda_pendentes':
            0,
    }

    # ========================================================
    # PROCESSA CADA EXCEÇÃO
    # ========================================================

    for exc in excecoes_passo1:

        linha_a = (
            exc['linha_a']
            .ljust(240)
        )

        linha_b = (
            exc['linha_b']
            .ljust(240)
        )

        cnpj_raw = exc.get(
            'cnpj',
            ''
        )

        nome = exc.get(
            'nome',
            ''
        )

        valor = exc.get(
            'valor',
            0
        )

        # ====================================================
        # LOCALIZA CNPJ NO DE-PARA
        # ====================================================

        cnpj_raiz = normalizar_cnpj(
            cnpj_raw
        )

        dados_corretos = mapa_depara.get(
            cnpj_raiz
        )

        # ====================================================
        # ENCONTRADO NO DE-PARA
        # ====================================================

        if dados_corretos:

            dados_corretos = dict(
                dados_corretos
            )

            dados_corretos[
                'cnpj_favorecido_raw'
            ] = cnpj_raw

            # ------------------------------------------------
            # SEGMENTO A
            # ------------------------------------------------

            linha_a_ok = build_segment_a(
                linha_a,
                dados_corretos
            )

            # ------------------------------------------------
            # SEGMENTO B
            # ------------------------------------------------

            linha_b_ok = build_segment_b(
                linha_b,
                dados_corretos
            )

            # ------------------------------------------------
            # ADICIONA AO LOTE
            # ------------------------------------------------

            registros_detalhe_corrigidos.append(
                linha_a_ok
            )

            registros_detalhe_corrigidos.append(
                linha_b_ok
            )

            log['corrigidos'] += 1

        # ====================================================
        # NÃO ENCONTRADO NO DE-PARA
        # ====================================================

        else:

            log['ainda_pendentes'] += 1

            dados_ainda_pendentes.append({

                'CNPJ/CPF Favorecido':
                    cnpj_raw,

                'Nome do Favorecido':
                    nome,

                'Valor (R$)':
                    f"{valor / 100:.2f}",

                'Status':
                    'Pendente',

                'Motivo':
                    'CNPJ não informado no de-para',
            })

    # ========================================================
    # NÃO HÁ NENHUM TED CORRIGIDO
    # ========================================================

    if not registros_detalhe_corrigidos:

        bytes_pronto = None

    # ========================================================
    # MONTA LOTE TED
    # ========================================================

    else:

        lote = _resequenciar_lote(
            registros_detalhe_corrigidos,
            header_lote_original,
            trailer_lote_original,
            num_lote=1
        )

        # ----------------------------------------------------
        # IMPORTANTE:
        #
        # Aqui NÃO existe mais:
        #
        # header arquivo
        # trailer arquivo
        #
        # O retorno é SOMENTE:
        #
        # 1
        # 3
        # 5
        # ----------------------------------------------------

        bytes_pronto = (
            "\r\n".join(lote)
            .encode('latin-1')
        )

    # ========================================================
    # EXCEL DE PENDÊNCIAS
    # ========================================================

    excel_bytes = None

    if dados_ainda_pendentes:

        df = pd.DataFrame(
            dados_ainda_pendentes
        )

        output = BytesIO()

        with pd.ExcelWriter(
            output,
            engine='xlsxwriter'
        ) as writer:

            df.to_excel(
                writer,
                index=False,
                sheet_name='Pendencias'
            )

            ws = writer.sheets[
                'Pendencias'
            ]

            for idx, col in enumerate(
                df.columns
            ):

                max_len = max(
                    df[col]
                    .astype(str)
                    .map(len)
                    .max(),
                    len(col)
                ) + 2

                ws.set_column(
                    idx,
                    idx,
                    max_len
                )

        excel_bytes = output.getvalue()

    # ========================================================
    # RETORNO
    # ========================================================

    return (
        bytes_pronto,
        log,
        excel_bytes
    )


# ============================================================
# EXTRAÇÃO DAS EXCEÇÕES DO ARQUIVO ORIGINAL
# ============================================================

def extrair_excecoes_do_arquivo_original(
    file_remessa,
    cnpjs_bloqueados
):
    """
    Extrai do CNAB original as exceções que não foram
    processadas pelo DDA.

    Retorna:

        excecoes
        header_arquivo
        header_lote
        trailer_lote
        trailer_arquivo

    O processador TED utiliza apenas:

        header_lote
        trailer_lote

    O APP utiliza:

        header_arquivo
        trailer_arquivo
    """

    # ========================================================
    # NORMALIZA CNPJs BLOQUEADOS
    # ========================================================

    cnpjs_norm = {
        normalizar_cnpj(c)
        for c in cnpjs_bloqueados
    }

    # ========================================================
    # LÊ ARQUIVO ORIGINAL
    # ========================================================

    linhas = (
        file_remessa
        .getvalue()
        .decode("latin-1")
        .splitlines()
    )

    linhas = [
        l
        for l in linhas
        if l.strip()
    ]

    # ========================================================
    # ESTRUTURAS
    # ========================================================

    excecoes = []

    header_arquivo = ""

    header_lote = ""

    trailer_lote = ""

    trailer_arquivo = ""

    # ========================================================
    # PERCORRE CNAB
    # ========================================================

    i = 0

    while i < len(linhas):

        linha = linhas[i]

        if len(linha) < 8:

            i += 1
            continue

        tipo = linha[7]

        # ====================================================
        # HEADER ARQUIVO
        # ====================================================

        if (
            tipo == "0"
            and not header_arquivo
        ):

            header_arquivo = (
                linha.ljust(240)
            )

            i += 1
            continue

        # ====================================================
        # HEADER LOTE
        # ====================================================

        elif (
            tipo == "1"
            and not header_lote
        ):

            header_lote = (
                linha.ljust(240)
            )

            i += 1
            continue

        # ====================================================
        # TRAILER LOTE
        # ====================================================

        elif (
            tipo == "5"
            and not trailer_lote
        ):

            trailer_lote = (
                linha.ljust(240)
            )

            i += 1
            continue

        # ====================================================
        # TRAILER ARQUIVO
        # ====================================================

        elif (
            tipo == "9"
            and not trailer_arquivo
        ):

            trailer_arquivo = (
                linha.ljust(240)
            )

            i += 1
            continue

        # ====================================================
        # SEGMENTO A
        # ====================================================

        elif (
            tipo == "3"
            and len(linha) > 13
            and linha[13] == "A"
        ):

            linha_a = (
                linha.ljust(240)
            )

            # ------------------------------------------------
            # PROCURA SEGMENTO B
            # ------------------------------------------------

            linha_b = ""

            if (
                i + 1 < len(linhas)
                and len(linhas[i + 1]) > 13
                and linhas[i + 1][13] == "B"
            ):

                linha_b = (
                    linhas[i + 1]
                    .ljust(240)
                )

            # ------------------------------------------------
            # CNPJ FAVORECIDO
            #
            # Segmento B:
            # posição 19-32
            #
            # Python:
            # 18:32
            # ------------------------------------------------

            cnpj_raw = (
                linha_b[18:32].strip()
                if linha_b
                else ""
            )

            cnpj_raiz = normalizar_cnpj(
                cnpj_raw
            )

            # =================================================
            # SOMENTE EXCEÇÕES
            # =================================================

            if cnpj_raiz in cnpjs_norm:

                # ---------------------------------------------
                # DADOS BANCÁRIOS
                # ---------------------------------------------

                banco = (
                    linha_a[20:23]
                    .strip()
                )

                agencia = (
                    linha_a[23:28]
                    .strip()
                )

                dv_agencia = (
                    linha_a[28]
                    .strip()
                )

                conta = (
                    linha_a[29:41]
                    .strip()
                )

                dv_conta = (
                    linha_a[41]
                    .strip()
                )

                dv_agencia_conta = (
                    linha_a[42]
                    .strip()
                )

                # ---------------------------------------------
                # VALOR
                # ---------------------------------------------

                campo_valor = (
                    linha_a[119:134]
                )

                if campo_valor.strip().isdigit():

                    valor = int(
                        campo_valor
                    )

                else:

                    valor = 0

                # ---------------------------------------------
                # ADICIONA EXCEÇÃO
                # ---------------------------------------------

                excecoes.append({

                    # -----------------------------------------
                    # LINHAS ORIGINAIS
                    # -----------------------------------------

                    "linha_a":
                        linha_a,

                    "linha_b":
                        linha_b,

                    # -----------------------------------------
                    # IDENTIFICAÇÃO
                    # -----------------------------------------

                    "cnpj":
                        cnpj_raw,

                    "nome":
                        linha_a[43:73].strip(),

                    # -----------------------------------------
                    # VALOR
                    # -----------------------------------------

                    "valor":
                        valor,

                    # -----------------------------------------
                    # DADOS BANCÁRIOS
                    # -----------------------------------------

                    "banco":
                        banco,

                    "agencia":
                        agencia,

                    "dv_agencia":
                        dv_agencia,

                    "conta":
                        conta,

                    "dv_conta":
                        dv_conta,

                    "dv_agencia_conta":
                        dv_agencia_conta,

                    # -----------------------------------------
                    # AUXILIARES
                    # -----------------------------------------

                    "agencia_num":
                        agencia,

                    "conta_num":
                        conta,
                })

            # ------------------------------------------------
            # PULA A + B
            # ------------------------------------------------

            if linha_b:

                i += 2

            else:

                i += 1

            continue

        elif (
            tipo == "3"
            and len(linha) > 18
            and linha[13] == "J"
            and linha[17:19] != "52"
        ):
            linha_j = linha.ljust(240)

            linha_j52 = ""
            if (
                i + 1 < len(linhas)
                and len(linhas[i + 1]) > 18
                and linhas[i + 1][13] == "J"
                and linhas[i + 1][17:19] == "52"
            ):
                linha_j52 = linhas[i + 1].ljust(240)

            cnpj_raw = linha_j52[76:91].strip() if linha_j52 else ""
            cnpj_raiz = normalizar_cnpj(cnpj_raw)

            if cnpj_raiz in cnpjs_norm:
                campo_valor = linha_j[99:114]
                valor = int(campo_valor) if campo_valor.strip().isdigit() else 0

                excecoes.append({
                    "linha_a": linha_j,
                    "linha_b": linha_j52,
                    "cnpj": cnpj_raw,
                    "nome": linha_j[61:91].strip(),
                    "valor": valor,
                    "banco": "", "agencia": "", "dv_agencia": "",
                    "conta": "", "dv_conta": "", "dv_agencia_conta": "",
                    "agencia_num": "", "conta_num": "",
                })

            i += 2 if linha_j52 else 1
            continue

        # ====================================================
        # PRÓXIMO REGISTRO
        # ====================================================
    
        i += 1

    # ========================================================
    # RETORNO
    # ========================================================

    return (
        excecoes,
        header_arquivo,
        header_lote,
        trailer_lote,
        trailer_arquivo
    )