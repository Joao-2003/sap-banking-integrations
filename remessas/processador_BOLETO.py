import requests
import re
from datetime import datetime, date
import pandas as pd
from io import BytesIO


# ============================================================
# CONVERSÃO LINHA DIGITÁVEL -> CÓDIGO DE BARRAS
# ============================================================

def linha_digitavel_para_codigo_barras(linha_digitavel):
    ld = re.sub(r'\D', '', linha_digitavel)

    if len(ld) != 47:
        return None

    return (
        ld[0:4]
        + ld[32:33]
        + ld[33:47]
        + ld[4:9]
        + ld[10:20]
        + ld[21:31]
    )


# ============================================================
# RAIZ CNPJ
# ============================================================

def get_raiz_cnpj(cnpj_str):
    clean = re.sub(r'\D', '', str(cnpj_str)).lstrip('0')

    if not clean:
        return ""

    return clean.zfill(14)[:8]


# ============================================================
# CONSULTA DDA SAP
# ============================================================

def buscar_dados_dda_api(url_api, user, password):

    try:
        response = requests.get(
            url_api + "?$format=json",
            auth=(user, password),
            timeout=15
        )

        response.raise_for_status()

        dados_sap = response.json()['d']['results']

    except Exception as e:
        raise Exception(f"Falha ao conectar no SAP: {e}")

    return interpretar_dda(dados_sap)


def interpretar_dda(dados_sap):
    """Converte o retorno da YY1_DDA (MemoLine com linha digitável, CNPJ, vencimento e valor) em faturas."""

    faturas_sap = []

    for item in dados_sap:

        memo = item.get('MemoLine', '')

        if not memo or 'DDA BARCODE' not in memo:
            continue

        linha_dig = memo[:47]

        codigo_barras = linha_digitavel_para_codigo_barras(linha_dig)

        if not codigo_barras:
            continue

        partes = memo.split(';')

        if len(partes) >= 3:

            apenas_numeros_bloco1 = re.sub(r'\D', '', partes[0])

            cnpj_fornecedor = apenas_numeros_bloco1[47:].lstrip('0')

            data_str = partes[1].strip()

            try:

                dt_vencimento = datetime.strptime(
                    data_str,
                    "%d%m%Y"
                ).date()

                valor_centavos = int(
                    partes[2].strip()
                )

            except:
                continue

            faturas_sap.append({
                'cnpj': cnpj_fornecedor,
                'data': dt_vencimento,
                'valor': valor_centavos,
                'barcode': codigo_barras,
                'utilizada': False
            })

    return faturas_sap


# ============================================================
# BUSCA INTELIGENTE DA FATURA
# ============================================================

def buscar_fatura_ia(
    faturas_sap,
    cnpj_beneficiario,
    dt_cnab,
    valor_cnab
):

    if not dt_cnab:
        return None

    raiz_arquivo = get_raiz_cnpj(
        cnpj_beneficiario
    )

    # --------------------------------------------------------
    # MATCH EXATO
    # --------------------------------------------------------

    match_exato = [
        f
        for f in faturas_sap
        if (
            not f['utilizada']
            and get_raiz_cnpj(f['cnpj']) == raiz_arquivo
            and abs(f['valor'] - valor_cnab) <= 100
            and f['data'] == dt_cnab
        )
    ]

    if match_exato:

        # Comportamento flexível e intencional: pequenas distorções
        # de centavos entre fornecedores com valores muito próximos
        # devem passar normalmente. Fica com a primeira fatura
        # candidata dentro da tolerância de R$ 1,00 na data exata.

        match_exato[0]['utilizada'] = True

        return match_exato[0]

    # --------------------------------------------------------
    # MATCH FLEXÍVEL
    # --------------------------------------------------------

    candidatos_flex = [
        f
        for f in faturas_sap
        if (
            not f['utilizada']
            and get_raiz_cnpj(f['cnpj']) == raiz_arquivo
            and abs(f['valor'] - valor_cnab) <= 100
        )
    ]

    validos = [
        f
        for f in candidatos_flex
        if (dt_cnab - f['data']).days <= 7
    ]

    if validos:

        # Mesma filosofia do match exato: pequenas distorções e
        # múltiplas faturas parecidas do mesmo fornecedor não
        # devem travar o pagamento. Fica com a primeira fatura
        # válida dentro da tolerância de valor e de data.

        validos[0]['utilizada'] = True

        return validos[0]

    return None


# ============================================================
# SEGMENTO J
# ============================================================

def build_segment_j(
    banco,
    lote,
    seq,
    barcode,
    nome_fav,
    data_venc,
    true_valor,
    doc_num
):

    banco = str(banco).strip().zfill(3)[:3]
    lote = str(lote).strip().zfill(4)[:4]
    seq = str(seq).strip().zfill(5)[:5]

    nome = str(nome_fav).strip().ljust(30)[:30]

    data_venc = str(data_venc).strip().zfill(8)[:8]

    doc_num = str(doc_num).strip().ljust(20)[:20]

    valor_str = str(true_valor).strip().zfill(15)[:15]

    zeros_15 = "0" * 15

    linha = (
        f"{banco}{lote}3{seq}J 00"
        + f"{barcode.ljust(44)[:44]}"
        + f"{nome}"
        + f"{data_venc}{valor_str}"
        + f"{zeros_15}{zeros_15}"
        + f"{data_venc}{valor_str}"
        + f"{zeros_15}"
        + f"{doc_num}"
        + (" " * 38)
    )

    return linha[:240].ljust(240)


# ============================================================
# TIPO DE INSCRIÇÃO
# ============================================================

def determinar_tipo_inscricao(documento_original):

    cru = re.sub(
        r'\D',
        '',
        str(documento_original)
    )

    if len(cru) > 14:
        cru = cru[-14:]

    limpo = cru.lstrip('0')

    # --------------------------------------------------------
    # CNPJ
    # --------------------------------------------------------

    if len(limpo) > 11 or len(cru) == 14:
        return "2"

    # --------------------------------------------------------
    # POSSÍVEL CNPJ COM PADDING
    # --------------------------------------------------------

    if len(limpo) >= 6:

        bloco_filial = limpo[-6:-2]

        if bloco_filial.startswith('000'):
            return "2"

    # --------------------------------------------------------
    # CPF
    # --------------------------------------------------------

    return "1"


# ============================================================
# SEGMENTO J-52
# ============================================================

def build_segment_j52(
    banco,
    lote,
    seq,
    cnpj_empresa_raw,
    nome_empresa,
    cnpj_api_raw,
    nome_fav_arquivo
):

    banco = str(banco).strip().zfill(3)[:3]

    lote = str(lote).strip().zfill(4)[:4]

    seq = str(seq).strip().zfill(5)[:5]

    # --------------------------------------------------------
    # PAGADOR
    # --------------------------------------------------------

    t_insc_pag = determinar_tipo_inscricao(
        cnpj_empresa_raw
    )

    num_insc_pag = (
        re.sub(
            r'\D',
            '',
            str(cnpj_empresa_raw)
        )
        .zfill(15)[-15:]
    )

    nome_pag = (
        str(nome_empresa)
        .strip()
        .ljust(40)[:40]
    )

    # --------------------------------------------------------
    # FAVORECIDO
    # --------------------------------------------------------

    t_insc_fav = determinar_tipo_inscricao(
        cnpj_api_raw
    )

    num_insc_fav = (
        re.sub(
            r'\D',
            '',
            str(cnpj_api_raw)
        )
        .zfill(15)[-15:]
    )

    nome_fav = (
        str(nome_fav_arquivo)
        .strip()
        .ljust(40)[:40]
    )

    # --------------------------------------------------------
    # GARANTIDOR
    # --------------------------------------------------------

    t_insc_gar = "0"

    num_insc_gar = "0" * 15

    nome_gar = " " * 40

    # --------------------------------------------------------
    # MONTAGEM
    # --------------------------------------------------------

    linha = (
        f"{banco}{lote}3{seq}J 0052"
        + f"{t_insc_pag}{num_insc_pag}{nome_pag}"
        + f"{t_insc_fav}{num_insc_fav}{nome_fav}"
        + f"{t_insc_gar}{num_insc_gar}{nome_gar}"
        + (" " * 53)
    )

    return linha[:240].ljust(240)


# ============================================================
# EXTRAIR SOMENTE O LOTE
#
# NOVA ARQUITETURA:
#
# O processador NÃO devolve mais:
#   registro 0 - header do arquivo
#   registro 9 - trailer do arquivo
#
# Ele devolve:
#   registro 1
#   registros 3
#   registro 5
#
# O APP será responsável pelo registro 0 e 9.
# ============================================================

def extrair_lotes_processados(linhas):

    lotes = []

    lote_atual = None

    for linha in linhas:

        linha = linha[:240].ljust(240)

        if len(linha) < 8:
            continue

        tipo_registro = linha[7]

        # ----------------------------------------------------
        # HEADER DE LOTE
        # ----------------------------------------------------

        if tipo_registro == '1':

            # Se já havia lote aberto, fecha a estrutura
            # anterior antes de iniciar outro.
            if lote_atual is not None:

                lotes.append(lote_atual)

            lote_atual = [linha]

        # ----------------------------------------------------
        # REGISTROS DO LOTE
        # ----------------------------------------------------

        elif tipo_registro in ('2', '3', '4'):

            if lote_atual is not None:
                lote_atual.append(linha)

        # ----------------------------------------------------
        # TRAILER DO LOTE
        # ----------------------------------------------------

        elif tipo_registro == '5':

            if lote_atual is not None:

                lote_atual.append(linha)

                lotes.append(lote_atual)

                lote_atual = None

        # ----------------------------------------------------
        # HEADER 0 / TRAILER 9
        #
        # São ignorados propositalmente.
        # ----------------------------------------------------

        elif tipo_registro in ('0', '9'):

            continue

        else:

            if lote_atual is not None:
                lote_atual.append(linha)

    # --------------------------------------------------------
    # SEGURANÇA
    # --------------------------------------------------------

    if lote_atual is not None:
        lotes.append(lote_atual)

    return lotes


# ============================================================
# PROCESSAMENTO PRINCIPAL
# ============================================================

def processar_remessa_sap(
    file_remessa,
    url_api,
    user,
    password
):

    # ========================================================
    # 1. CONSULTA DDA
    # ========================================================

    faturas_sap = buscar_dados_dda_api(
        url_api,
        user,
        password
    )

    # ========================================================
    # 2. LEITURA CNAB ORIGINAL
    # ========================================================

    linhas_cnab = (
        file_remessa
        .getvalue()
        .decode('latin-1')
        .splitlines()
    )

    linhas_cnab = [
        l
        for l in linhas_cnab
        if l.strip()
    ]

    # ========================================================
    # 3. ESTRUTURAS
    # ========================================================

    arquivo_modificado = []

    dados_bloqueados = []

    log = {
        'aprovados': 0,
        'bloqueados': 0,
        'faturas_bloqueadas_info': [],
        'cnpjs_bloqueados': [],
        'aprovados_info': [],
        'mapa_tamanho': len(faturas_sap)
    }


    lote_atual_qtd = 0

    seq_lote = 1

    # ========================================================
    # 4. IDENTIFICA HEADER DO LOTE
    #
    # Continua sendo usado para obter:
    # - CNPJ da empresa
    # - Nome da empresa
    #
    # O header será devolvido dentro do lote.
    # ========================================================

    header_lote = next(
        (
            l
            for l in linhas_cnab
            if len(l) > 200
            and l[7] == '1'
        ),
        None
    )

    if header_lote:

        sujeira = header_lote[18:32]

        cnpj_empresa_limpo = re.sub(
            r'\D',
            '',
            sujeira
        )

        nome_empresa = header_lote[72:112].strip()

    else:

        cnpj_empresa_limpo = "0"

        nome_empresa = " "

    # ========================================================
    # 5. PROCESSAMENTO
    # ========================================================

    i = 0

    while i < len(linhas_cnab):

        linha = linhas_cnab[i]

        tipo_registro = (
            " "
            if len(linha) < 8
            else linha[7]
        )

        # ====================================================
        # HEADER DO ARQUIVO
        #
        # NÃO entra mais no retorno final do BOLETO.
        #
        # O APP preservará o header original.
        # ====================================================

        if tipo_registro == '0':

            i += 1

        # ====================================================
        # HEADER DO LOTE
        # ====================================================

        elif tipo_registro == '1':

            # Mantém exatamente a mesma regra existente:
            # posições 11-12 = 30
            linha_mod = (
                linha[:11]
                + '30'
                + linha[13:]
            )

            arquivo_modificado.append(
                linha_mod
            )

            lote_atual_qtd = 0

            seq_lote = 1

            i += 1

        # ====================================================
        # DETALHES
        # ====================================================

        elif tipo_registro == '3':

            segmento = linha[13]

            # =================================================
            # SEGMENTO J
            # =================================================

            if segmento == 'J':

                linha_J = linha.ljust(240)

                linha_J52 = (
                    linhas_cnab[i + 1].ljust(240)
                    if (
                        i + 1 < len(linhas_cnab)
                        and linhas_cnab[i + 1][13:17] == 'J 00'
                    )
                    else None
                )

                if linha_J52:

                    banco = linha_J[0:3]

                    lote = linha_J[3:7]

                    dt_cnab_str = linha_J[91:99]

                    valor_cnab = int(
                        linha_J[99:114]
                    )

                    cnpj_beneficiario = (
                        linha_J52[76:91]
                        .strip()
                    )

                    nome_favorecido = (
                        linha_J[61:91]
                        .strip()
                    )

                    doc_num = (
                        linha_J[182:202]
                        .strip()
                    )

                    try:

                        dt_cnab = datetime.strptime(
                            dt_cnab_str,
                            "%d%m%Y"
                        ).date()

                    except:

                        dt_cnab = None

                    # -----------------------------------------
                    # PROCURA NO DDA
                    # -----------------------------------------

                    fatura_encontrada = buscar_fatura_ia(
                        faturas_sap,
                        cnpj_beneficiario,
                        dt_cnab,
                        valor_cnab
                    )

                    # -----------------------------------------
                    # ENCONTRADO
                    # -----------------------------------------

                    if fatura_encontrada:

                        barcode_novo = (
                            fatura_encontrada['barcode']
                        )

                        cnpj_api = (
                            fatura_encontrada['cnpj']
                        )

                        true_valor = (
                            fatura_encontrada['valor']
                        )

                        # -------------------------------------
                        # NOVO SEGMENTO J
                        # -------------------------------------

                        linha_J_pronta = build_segment_j(
                            banco,
                            lote,
                            seq_lote,
                            barcode_novo,
                            nome_favorecido,
                            dt_cnab_str,
                            true_valor,
                            doc_num
                        )

                        seq_lote += 1

                        # -------------------------------------
                        # NOVO J-52
                        # -------------------------------------

                        linha_J52_pronta = build_segment_j52(
                            banco,
                            lote,
                            seq_lote,
                            cnpj_empresa_limpo,
                            nome_empresa,
                            cnpj_api,
                            nome_favorecido
                        )

                        seq_lote += 1

                        arquivo_modificado.extend([
                            linha_J_pronta,
                            linha_J52_pronta
                        ])

                        log['aprovados'] += 1

                        lote_atual_qtd += 2

                    # -----------------------------------------
                    # NÃO ENCONTRADO
                    # -----------------------------------------

                    else:

                        log['bloqueados'] += 1

                        log[
                            'faturas_bloqueadas_info'
                        ].append(
                            f"Fornecedor: {nome_favorecido} | "
                            f"CNPJ: {cnpj_beneficiario} | "
                            f"Valor: R$ {valor_cnab / 100:.2f}"
                        )

                        log['cnpjs_bloqueados'].append(cnpj_beneficiario)
                        
                        dados_bloqueados.append({

                            'CNPJ/Fornecedor':
                                cnpj_beneficiario,

                            'Nome do Favorecido':
                                nome_favorecido,

                            'Valor Original (R$)':
                                valor_cnab / 100.0,

                            'Vencimento SAP':
                                (
                                    dt_cnab.strftime("%d/%m/%Y")
                                    if dt_cnab
                                    else 'Inválido'
                                ),

                            'Tipo Lançamento':
                                'Segmento J (Boleto)',

                            'Status':
                                'Bloqueado',

                            'Motivo da Ocorrência':
                                'Não encontrado no DDA da API '
                                '(Tolerância: R$ 1,00 / 7 dias)'
                        })

                    i += 2

                else:

                    arquivo_modificado.append(
                        linha
                    )

                    lote_atual_qtd += 1

                    seq_lote += 1

                    i += 1

            # =================================================
            # SEGMENTO A
            # =================================================

            elif segmento == 'A':

                linha_A = linha.ljust(240)

                linha_B = (
                    linhas_cnab[i + 1].ljust(240)
                    if (
                        i + 1 < len(linhas_cnab)
                        and linhas_cnab[i + 1][13] == 'B'
                    )
                    else None
                )

                if linha_B:

                    banco = linha_A[0:3]

                    lote = linha_A[3:7]

                    dt_cnab_str = linha_A[93:101]

                    valor_cnab = int(
                        linha_A[119:134]
                    )

                    cnpj_beneficiario = (
                        linha_B[18:32]
                        .strip()
                    )

                    nome_favorecido = (
                        linha_A[43:73]
                        .strip()
                    )

                    doc_num = (
                        linha_A[73:93]
                        .strip()
                    )

                    try:

                        dt_cnab = datetime.strptime(
                            dt_cnab_str,
                            "%d%m%Y"
                        ).date()

                    except:

                        dt_cnab = None

                    # -----------------------------------------
                    # PROCURA NO DDA
                    # -----------------------------------------

                    fatura_encontrada = buscar_fatura_ia(
                        faturas_sap,
                        cnpj_beneficiario,
                        dt_cnab,
                        valor_cnab
                    )

                    # -----------------------------------------
                    # ENCONTRADO
                    # -----------------------------------------

                    if fatura_encontrada:

                        barcode_novo = (
                            fatura_encontrada['barcode']
                        )

                        cnpj_api = (
                            fatura_encontrada['cnpj']
                        )

                        true_valor = (
                            fatura_encontrada['valor']
                        )

                        # -------------------------------------
                        # NOVO SEGMENTO J
                        # -------------------------------------

                        linha_J_pronta = build_segment_j(
                            banco,
                            lote,
                            seq_lote,
                            barcode_novo,
                            nome_favorecido,
                            dt_cnab_str,
                            true_valor,
                            doc_num
                        )

                        seq_lote += 1

                        # -------------------------------------
                        # NOVO J-52
                        # -------------------------------------

                        linha_J52_pronta = build_segment_j52(
                            banco,
                            lote,
                            seq_lote,
                            cnpj_empresa_limpo,
                            nome_empresa,
                            cnpj_api,
                            nome_favorecido
                        )

                        seq_lote += 1

                        arquivo_modificado.extend([
                            linha_J_pronta,
                            linha_J52_pronta
                        ])

                        log['aprovados'] += 1

                        log['aprovados_info'].append({
                            'nome_favorecido': nome_favorecido,
                            'cnpj_beneficiario': cnpj_beneficiario,
                            'valor_original': valor_cnab / 100.0,
                            'valor_corrigido': true_valor / 100.0,
                            'delta': (true_valor - valor_cnab) / 100.0,
                            'vencimento': (
                                dt_cnab.strftime("%d/%m/%Y")
                                if dt_cnab
                                else 'Inválido'
                            ),
                            'doc_num': doc_num,
                        })

                        lote_atual_qtd += 2

                    # -----------------------------------------
                    # NÃO ENCONTRADO
                    # -----------------------------------------

                    else:

                        log['bloqueados'] += 1

                        log[
                            'faturas_bloqueadas_info'
                        ].append(
                            f"Fornecedor: {nome_favorecido} | "
                            f"CNPJ: {cnpj_beneficiario} | "
                            f"Valor: R$ {valor_cnab / 100:.2f}"
                        )

                        log['cnpjs_bloqueados'].append(cnpj_beneficiario)

                        dados_bloqueados.append({

                            'CNPJ/Fornecedor':
                                cnpj_beneficiario,

                            'Nome do Favorecido':
                                nome_favorecido,

                            'Valor Original (R$)':
                                valor_cnab / 100.0,

                            'Vencimento SAP':
                                (
                                    dt_cnab.strftime("%d/%m/%Y")
                                    if dt_cnab
                                    else 'Inválido'
                                ),

                            'Tipo Lançamento':
                                'Segmento A (Transferência)',

                            'Status':
                                'Bloqueado',

                            'Motivo da Ocorrência':
                                'Não encontrado no DDA da API '
                                '(Tolerância: R$ 1,00 / 7 dias)'
                        })

                    i += 2

                else:

                    arquivo_modificado.append(
                        linha
                    )

                    lote_atual_qtd += 1

                    seq_lote += 1

                    i += 1

            # =================================================
            # OUTROS SEGMENTOS
            # =================================================

            else:

                linha_mod = (
                    linha[:8]
                    + str(seq_lote).zfill(5)
                    + linha[13:]
                )

                arquivo_modificado.append(
                    linha_mod
                )

                lote_atual_qtd += 1

                seq_lote += 1

                i += 1

        elif tipo_registro == '5':

            # ========================================================
            # TOTAL DO LOTE BOLETO
            #
            # Soma diretamente os valores dos Segmentos J
            # efetivamente criados no lote.
            #
            # CNAB:
            #   posições 100-114 = Valor do Título
            #
            # Python:
            #   [99:114]
            # ========================================================

            valor_total_lote = 0

            for registro in arquivo_modificado:

                registro = registro[:240].ljust(240)

                if (
                    registro[7] == '3'
                    and registro[13] == 'J'
                ):

                    valor_txt = registro[99:114].strip()

                    try:
                        valor_total_lote += int(
                            valor_txt or 0
                        )
                    except ValueError:
                        pass

            # ========================================================
            # QUANTIDADE DE REGISTROS
            #
            # lote_atual_qtd:
            #   somente os registros de detalhe
            #
            # +2:
            #   header do lote + trailer do lote
            # ========================================================

            total_registros_lote = (
                lote_atual_qtd + 2
            )

            # ========================================================
            # MONTA TRAILER
            # ========================================================

            linha_mod = (
                linha[:17]
                + str(total_registros_lote).zfill(6)
                + str(valor_total_lote).zfill(18)
                + linha[41:]
            )

            arquivo_modificado.append(
                linha_mod
            )

            i += 1

        # ====================================================
        # TRAILER DO ARQUIVO
        #
        # NÃO É MAIS DEVOLVIDO.
        #
        # O APP vai criar o registro 9 final.
        # ====================================================

        elif tipo_registro == '9':

            i += 1

        # ====================================================
        # OUTROS
        # ====================================================

        else:

            arquivo_modificado.append(
                linha
            )

            i += 1

    # ========================================================
    # 6. STATUS DE FATURAS NÃO UTILIZADAS HOJE
    # ========================================================

    log['esquecidas_hoje'] = sum(
        1
        for f in faturas_sap
        if (
            not f['utilizada']
            and f['data'] == date.today()
        )
    )

    # ========================================================
    # 7. EXCEL DE BLOQUEIOS
    # ========================================================

    excel_bytes = None

    if dados_bloqueados:

        df = pd.DataFrame(
            dados_bloqueados
        )

        output = BytesIO()

        with pd.ExcelWriter(
            output,
            engine='xlsxwriter'
        ) as writer:

            df.to_excel(
                writer,
                index=False,
                sheet_name='Ocorrencias Bloqueadas'
            )

            worksheet = writer.sheets[
                'Ocorrencias Bloqueadas'
            ]

            for indice, col in enumerate(
                df.columns
            ):

                max_len = max(
                    df[col]
                    .astype(str)
                    .map(len)
                    .max(),
                    len(col)
                ) + 2

                worksheet.set_column(
                    indice,
                    indice,
                    max_len
                )

        excel_bytes = output.getvalue()

    # ========================================================
    # 8. NOVA ARQUITETURA
    #
    # Aqui temos:
    #
    #   1 - Header do lote
    #   3 - Detalhes
    #   5 - Trailer do lote
    #
    # O header 0 e trailer 9 foram removidos.
    # ========================================================

    lotes_processados = extrair_lotes_processados(
        arquivo_modificado
    )

    # ========================================================
    # 9. VALIDAÇÃO
    #
    # O desenho novo trabalha com:
    #
    #   1 lote = BOLETO/DDA
    #
    # Portanto esperamos no máximo um lote.
    # ========================================================

    if len(lotes_processados) == 0:

        bytes_pronto = b""

    elif len(lotes_processados) == 1:

        bytes_pronto = (
            "\r\n".join(
                lotes_processados[0]
            )
            .encode('latin-1')
        )

    else:

        raise Exception(
            "O processamento de BOLETO retornou "
            f"{len(lotes_processados)} lotes. "
            "A nova arquitetura permite apenas "
            "1 lote de BOLETO/DDA."
        )

    # ========================================================
    # 10. RETORNO
    #
    # Continua com 3 retornos para não quebrar o APP:
    #
    #   bytes_pronto
    #   log
    #   excel_bytes
    #
    # Porém bytes_pronto agora contém SOMENTE O LOTE.
    # ========================================================

    return (
        bytes_pronto,
        log,
        excel_bytes
    )