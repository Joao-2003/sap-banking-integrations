"""
============================================================
PDF DO BOLETO SANTANDER
============================================================

O Santander não devolve PDF nem URL do boleto: a documentação
orienta o cliente a montar a imagem com os dados do registro.
O layout segue o boleto emitido pelo próprio Santander
(Internet Banking): Recibo do Pagador + Ficha de Compensação.

    from boleto_pdf_santander import gerar_pdf_boleto
    caminho, teste = gerar_pdf_boleto(payload, resposta, beneficiario, pasta, nome_arquivo)

Validações antes de desenhar (o PDF só é gerado se passarem):
  - código de barras com 44 dígitos, banco 033 e DV geral correto;
  - linha digitável igual à derivada do código de barras.

Se o valor embutido no código de barras for diferente do valor
do título, ou o registro for do ambiente TESTE, o PDF é gerado
com a marca "DOCUMENTO DE TESTE - NÃO PAGAR".

Dependência: pip install reportlab
"""

import math
import re
from datetime import date, datetime
from pathlib import Path

from reportlab.graphics import renderPDF
from reportlab.graphics.barcode.common import I2of5
from reportlab.graphics.barcode.qr import QrCodeWidget
from reportlab.graphics.shapes import Drawing
from reportlab.lib.colors import Color, black
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.pdfgen import canvas

BANCO_NOME = "Santander"
BANCO_CODIGO = "033-7"
LOCAL_PAGAMENTO = "PAGAVEL PREFERENCIALMENTE NO BANCO SANTANDER"
# Texto da carteira como o Santander imprime. 104 confirmado em boleto real;
# 101 (registro via API) confirmar numa 2ª via do Internet Banking.
CARTEIRAS = {"101": "RAPIDA C/REG", "104": "ELETR C/REG"}

LARGURA_PAGINA, ALTURA_PAGINA = A4
MARGEM = 10 * mm
LARGURA = LARGURA_PAGINA - 2 * MARGEM
COL_DIREITA = 45 * mm
COL_ESQUERDA = LARGURA - COL_DIREITA
H = 8.5 * mm

FONTE = "Helvetica"
FONTE_N = "Helvetica-Bold"


# ============================================================
# CÓDIGO DE BARRAS / LINHA DIGITÁVEL
# ============================================================

def _mod10(numero):
    soma, peso = 0, 2
    for digito in reversed(numero):
        r = int(digito) * peso
        soma += r // 10 + r % 10
        peso = 1 if peso == 2 else 2
    return (10 - soma % 10) % 10


def _mod11_codigo_barras(numero):
    soma, peso = 0, 2
    for digito in reversed(numero):
        soma += int(digito) * peso
        peso = 2 if peso == 9 else peso + 1
    resto = 11 - soma % 11
    return 1 if resto in (0, 1, 10, 11) else resto


def linha_digitavel_de(codigo_barras):
    c = codigo_barras
    campo1 = c[0:4] + c[19:24]
    campo2 = c[24:34]
    campo3 = c[34:44]
    return (
        campo1 + str(_mod10(campo1))
        + campo2 + str(_mod10(campo2))
        + campo3 + str(_mod10(campo3))
        + c[4] + c[5:19]
    )


def formatar_linha_digitavel(linha):
    """Formato usado pelo Santander: 03399.00000.00000.000000.00000.000000.0.00000000000000"""
    l = re.sub(r"\D", "", linha or "")
    if len(l) != 47:
        return linha
    return f"{l[0:5]}.{l[5:10]}.{l[10:15]}.{l[15:21]}.{l[21:26]}.{l[26:32]}.{l[32]}.{l[33:47]}"


def validar_codigo_barras(codigo_barras, linha_digitavel):
    c = re.sub(r"\D", "", codigo_barras or "")
    l = re.sub(r"\D", "", linha_digitavel or "")

    if len(c) != 44:
        raise ValueError(f"código de barras com {len(c)} dígitos (esperado 44)")
    if not c.startswith("033"):
        raise ValueError("código de barras não é do Santander (033)")
    if int(c[4]) != _mod11_codigo_barras(c[:4] + c[5:]):
        raise ValueError("dígito verificador geral do código de barras inválido")
    if l and l != linha_digitavel_de(c):
        raise ValueError("linha digitável não corresponde ao código de barras")

    return c


# ============================================================
# FORMATAÇÃO
# ============================================================

def _moeda(valor):
    if valor in (None, ""):
        return ""
    texto = f"{float(valor):,.2f}"
    return texto.replace(",", "X").replace(".", ",").replace("X", ".")


def _data(valor):
    if not valor:
        return ""
    try:
        return datetime.strptime(str(valor)[:10], "%Y-%m-%d").strftime("%d/%m/%Y")
    except ValueError:
        return str(valor)


def _documento(numero, tipo):
    n = re.sub(r"\D", "", str(numero or ""))
    if tipo == "CNPJ" and len(n) == 14:
        return f"{n[:2]}.{n[2:5]}.{n[5:8]}/{n[8:12]}-{n[12:]}"
    if tipo == "CPF" and len(n) == 11:
        return f"{n[:3]}.{n[3:6]}.{n[6:9]}-{n[9:]}"
    return n


def _truncar_centavos(valor):
    return math.floor(valor * 100 + 1e-9) / 100


def _instrucoes(payload):
    """Instruções no padrão do Santander (maiúsculas, sem acento)."""
    linhas = []
    valor = float(payload.get("nominalValue") or 0)

    juros = payload.get("interestPercentage")
    if juros not in (None, "") and float(juros) > 0:
        diario = _truncar_centavos(valor * float(juros) / 100 / 30)
        linhas.append(f"JUROS DIARIO DE R$ {_moeda(diario)}")

    multa = payload.get("finePercentage")
    if multa not in (None, "") and float(multa) > 0:
        dias = payload.get("fineQuantityDays")
        apos = f" APOS {dias} DIA(S) DO VENCIMENTO" if dias else " APOS O VENCIMENTO"
        linhas.append(f"MULTA DE {_moeda(multa)}%{apos}")

    if payload.get("protestType") in ("DIAS_CORRIDOS", "DIAS_UTEIS"):
        tipo = "CORRIDOS" if payload["protestType"] == "DIAS_CORRIDOS" else "UTEIS"
        linhas.append(
            f"SUJEITO A PROTESTO APOS {payload.get('protestQuantityDays')} DIAS {tipo} DO VENCIMENTO")

    baixa = str(payload.get("writeOffQuantityDays") or "")
    if baixa.isdigit() and 0 < int(baixa) < 99:
        linhas.append(f"NAO RECEBER APOS {int(baixa)} DIAS DO VENCIMENTO")

    for mensagem in payload.get("messages") or []:
        if str(mensagem).strip():
            linhas.append(str(mensagem).strip().upper()[:100])

    return linhas[:7]


# ============================================================
# DESENHO
# ============================================================

class _Desenho:

    def __init__(self, c):
        self.c = c

    def cortar(self, texto, largura, fonte, tamanho):
        texto = str(texto)
        while texto and self.c.stringWidth(texto, fonte, tamanho) > largura:
            texto = texto[:-1]
        return texto

    def campo(self, x, y, largura, altura, rotulo, valor="", alinhar="centro", tamanho=8, borda=True):
        c = self.c
        if borda:
            c.setLineWidth(0.4)
            c.line(x, y, x + largura, y)
            c.line(x + largura, y, x + largura, y + altura)
        c.setFont(FONTE, 6)
        c.drawString(x + 1.2 * mm, y + altura - 2.4 * mm, rotulo)
        if valor != "":
            c.setFont(FONTE_N, tamanho)
            texto = self.cortar(valor, largura - 3 * mm, FONTE_N, tamanho)
            base = y + 1.4 * mm
            if alinhar == "direita":
                c.drawRightString(x + largura - 1.5 * mm, base, texto)
            elif alinhar == "esquerda":
                c.drawString(x + 1.5 * mm, base, texto)
            else:
                c.drawCentredString(x + largura / 2, base, texto)

    def cabecalho(self, y, direita_texto, direita_tamanho):
        """Linha do banco: nome | 033-7 | texto à direita."""
        c = self.c
        altura = 9 * mm
        c.setFont(FONTE_N, 14)
        c.drawString(MARGEM + 1 * mm, y + 2.4 * mm, BANCO_NOME)
        x_cod = MARGEM + 35 * mm
        c.setLineWidth(1.2)
        c.line(x_cod + 26 * mm, y + 0.5 * mm, x_cod + 26 * mm, y + altura)
        c.setFont(FONTE_N, 15)
        c.drawString(x_cod + 3 * mm, y + 2.2 * mm, BANCO_CODIGO)
        c.setFont(FONTE_N, direita_tamanho)
        c.drawRightString(MARGEM + LARGURA, y + 2.2 * mm, direita_texto)
        c.setLineWidth(1.6)
        c.line(MARGEM, y, MARGEM + LARGURA, y)
        return altura


def _bloco_campos(d, y, dados):
    """Linhas comuns ao recibo e à ficha (Local de pagamento até Valor do Documento).
    Recebe o y do topo do bloco e devolve o y da base."""
    x0 = MARGEM

    y -= H
    d.campo(x0, y, COL_ESQUERDA, H, "Local de Pagamento", LOCAL_PAGAMENTO, "centro", 7.5)
    d.campo(x0 + COL_ESQUERDA, y, COL_DIREITA, H, "Vencimento", dados["vencimento"], "direita", 10)

    altura_benef = 11 * mm
    y -= altura_benef
    d.campo(x0, y, COL_ESQUERDA, altura_benef, "Beneficiário", "")
    c = d.c
    c.setFont(FONTE_N, 7)
    c.drawString(x0 + 1.5 * mm, y + 5.0 * mm,
                 d.cortar(dados["beneficiario_linha"], COL_ESQUERDA - 3 * mm, FONTE_N, 7))
    if dados["beneficiario_endereco"]:
        c.drawString(x0 + 1.5 * mm, y + 1.5 * mm,
                     d.cortar(dados["beneficiario_endereco"], COL_ESQUERDA - 3 * mm, FONTE_N, 7))
    d.campo(x0 + COL_ESQUERDA, y, COL_DIREITA, altura_benef, "Agência / Cod. Beneficiário",
            dados["agencia_codigo"], "direita", 8)

    y -= H
    larg = [28 * mm, 30 * mm, 24 * mm, 23 * mm, COL_ESQUERDA - 105 * mm]
    rot = ["Data do Documento", "No. do Documento", "Espécie doc.", "Aceite", "Data Processamento"]
    val = [dados["emissao"], dados["numero_documento"], "DM", "NAO ACEITO", dados["processamento"]]
    x = x0
    for w, r, v in zip(larg, rot, val):
        d.campo(x, y, w, H, r, v, "centro", 7.5)
        x += w
    d.campo(x0 + COL_ESQUERDA, y, COL_DIREITA, H, "Nosso Número", dados["nosso_numero"], "direita", 8)

    y -= H
    rot = ["Uso do Banco", "Carteira", "Espécie Moeda", "Quantidade", "(x) Valor"]
    val = ["", dados["carteira"], "REAL", "", "R$ 0,00"]
    x = x0
    for w, r, v in zip(larg, rot, val):
        d.campo(x, y, w, H, r, v, "centro", 7.5)
        x += w
    d.campo(x0 + COL_ESQUERDA, y, COL_DIREITA, H, "(=) Valor do Documento",
            f"R$ {dados['valor']}", "direita", 10)
    return y


def _pagador(d, y, dados, rotulo_final):
    c = d.c
    x0 = MARGEM
    y -= 10 * mm
    c.setFont(FONTE, 6.5)
    c.drawString(x0 + 1 * mm, y + 6.5 * mm, "Pagador")
    c.setFont(FONTE, 7)
    c.drawString(x0 + 30 * mm, y + 6.5 * mm, d.cortar(dados["pagador_linha1"], LARGURA - 32 * mm, FONTE, 7))
    c.drawString(x0 + 30 * mm, y + 3.2 * mm, d.cortar(dados["pagador_linha2"], LARGURA - 32 * mm, FONTE, 7))
    c.setFont(FONTE, 6.5)
    c.drawString(x0 + 1 * mm, y, rotulo_final)
    return y


def _qr(c, conteudo, x, y, tamanho):
    widget = QrCodeWidget(conteudo)
    x0, y0, x1, y1 = widget.getBounds()
    desenho = Drawing(tamanho, tamanho, transform=[tamanho / (x1 - x0), 0, 0, tamanho / (y1 - y0), 0, 0])
    desenho.add(widget)
    renderPDF.draw(desenho, c, x, y)


def _marca_teste(c):
    c.saveState()
    c.setFillColor(Color(0.85, 0, 0, alpha=0.25))
    c.setFont(FONTE_N, 44)
    c.translate(LARGURA_PAGINA / 2, ALTURA_PAGINA / 2)
    c.rotate(35)
    c.drawCentredString(0, 20, "DOCUMENTO DE TESTE")
    c.drawCentredString(0, -30, "NAO PAGAR")
    c.restoreState()


# ============================================================
# FUNÇÃO PRINCIPAL
# ============================================================

def gerar_pdf_boleto(payload, resposta, beneficiario, pasta="boletos_santander", nome_arquivo=None):
    """
    payload      : dict enviado ao Santander (payer, nominalValue, dueDate...)
    resposta     : dict devolvido pelo Santander (barCode, digitableLine, qrCodePix...)
    beneficiario : {"nome", "cnpj", "endereco", "agencia", "pix": bool}
    nome_arquivo : nome do PDF (ex.: CLIENTE_NF_36956-1.pdf); se None, usa o nosso número
    Retorna (caminho do PDF, marcado_como_teste).
    """
    # Produção devolve "barCode"; sandbox/documentação usam "barcode"
    codigo_barras = validar_codigo_barras(
        resposta.get("barCode") or resposta.get("barcode"),
        resposta.get("digitableLine"),
    )
    linha = formatar_linha_digitavel(resposta.get("digitableLine") or linha_digitavel_de(codigo_barras))

    valor_titulo = float(payload["nominalValue"])
    valor_codigo = int(codigo_barras[9:19]) / 100
    teste = payload.get("environment") == "TESTE" or abs(valor_codigo - valor_titulo) > 0.005

    carteira = codigo_barras[41:44]
    pagador = payload["payer"]
    agencia = str(beneficiario.get("agencia", "")).strip()

    dados = {
        "vencimento": _data(payload["dueDate"]),
        "beneficiario_linha": f"{beneficiario['nome']} - {_documento(beneficiario['cnpj'], 'CNPJ')}",
        "beneficiario_endereco": beneficiario.get("endereco", ""),
        "agencia_codigo": f"{agencia} / {codigo_barras[20:27]}" if agencia else codigo_barras[20:27],
        "emissao": _data(payload["issueDate"]),
        "numero_documento": payload.get("clientNumber", ""),
        "processamento": _data(resposta.get("entryDate")) or date.today().strftime("%d/%m/%Y"),
        # O Santander grava o bankNumber como veio (sem DV): 13 dígitos do código de barras
        "nosso_numero": codigo_barras[27:40],
        "carteira": CARTEIRAS.get(carteira, carteira),
        "valor": _moeda(valor_titulo),
        "pagador_linha1": f"{pagador['name']} {_documento(pagador['documentNumber'], pagador['documentType'])}",
        "pagador_linha2": (f"{pagador['address']} {pagador.get('neighborhood', '')} "
                           f"{pagador['zipCode']} - {pagador['city']} - {pagador['state']}"),
    }
    instrucoes = _instrucoes(payload)
    qr_pix = resposta.get("qrCodePix") if beneficiario.get("pix") else None

    pasta = Path(pasta)
    pasta.mkdir(parents=True, exist_ok=True)
    nome = nome_arquivo or f"boleto_santander_{payload['bankNumber']}_{dados['numero_documento']}.pdf"
    arquivo = pasta / nome

    c = canvas.Canvas(str(arquivo), pagesize=A4)
    c.setTitle(f"Boleto Santander {dados['numero_documento']}")
    c.setAuthor(beneficiario["nome"])
    d = _Desenho(c)
    x0 = MARGEM

    # ------------------------------------------------ RECIBO DO PAGADOR
    y = ALTURA_PAGINA - MARGEM - 9 * mm
    d.cabecalho(y, "RECIBO DO PAGADOR", 12)
    y = _bloco_campos(d, y, dados)
    y = _pagador(d, y, dados, "Beneficiário Final")

    c.setLineWidth(0.4)
    c.line(x0, y - 1.5 * mm, x0 + LARGURA, y - 1.5 * mm)
    y -= 5 * mm
    c.setFont(FONTE, 6.5)
    c.drawString(x0 + 1 * mm, y, "Mensagem / Instruções (Texto de Responsabilidade do Beneficiário)")
    c.setFont(FONTE, 7)
    for i, texto in enumerate(instrucoes):
        c.drawString(x0 + 1 * mm, y - (3.5 + i * 3.4) * mm, texto)

    if qr_pix:
        tamanho = 32 * mm
        x_qr = x0 + LARGURA - tamanho
        y_qr = y - 3 * mm - tamanho
        _qr(c, qr_pix, x_qr, y_qr, tamanho)
        c.setFont(FONTE_N, 8)
        c.drawRightString(x_qr - 3 * mm, y_qr + tamanho - 6 * mm, "PAGUE TAMBEM COM PIX")
        c.setFont(FONTE, 6.5)
        c.drawRightString(x_qr - 3 * mm, y_qr + tamanho - 10 * mm, "Aponte a câmera do app do seu banco para o QR Code.")
        c.drawRightString(x_qr - 3 * mm, y_qr + tamanho - 13.5 * mm, "Mesmo valor e vencimento deste boleto.")

    # ------------------------------------------------ CORTE
    y_corte = ALTURA_PAGINA * 0.47
    c.setLineWidth(0.8)
    c.line(x0, y_corte + 6 * mm, x0 + LARGURA, y_corte + 6 * mm)
    c.setFont(FONTE, 6.5)
    c.drawRightString(x0 + LARGURA - 30 * mm, y_corte + 3 * mm, "Autenticação Mecânica")
    c.setDash(3, 2)
    c.setLineWidth(0.5)
    c.line(x0, y_corte, x0 + LARGURA, y_corte)
    c.setDash()
    c.setFont(FONTE, 6.5)
    c.drawRightString(x0 + LARGURA - 8 * mm, y_corte + 0.8 * mm, "Corte na Linha Pontilhada")

    # ------------------------------------------------ FICHA DE COMPENSAÇÃO
    y = y_corte - 5 * mm - 9 * mm
    d.cabecalho(y, linha, 10.5)
    y = _bloco_campos(d, y, dados)

    rotulos = ["(-) Descontos/Abatimento", "(+) Mora/Multa", "(=) Valor Cobrado"]
    valores = ["R$ 0,00", "R$ 0,00", ""]
    altura_instr = H * len(rotulos)
    y -= altura_instr
    c.setLineWidth(0.4)
    c.line(x0, y, x0 + COL_ESQUERDA, y)
    c.setFont(FONTE, 6)
    c.drawString(x0 + 1.2 * mm, y + altura_instr - 2.4 * mm, "Instruções")
    c.setFont(FONTE_N, 7)
    for i, texto in enumerate(instrucoes[:6]):
        c.drawString(x0 + 1.5 * mm, y + altura_instr - (6 + i * 3.3) * mm, texto)
    for i, (rotulo, valor) in enumerate(zip(rotulos, valores)):
        d.campo(x0 + COL_ESQUERDA, y + altura_instr - (i + 1) * H, COL_DIREITA, H, rotulo, valor,
                "esquerda", 7)

    c.setLineWidth(0.8)
    c.line(x0, y, x0 + LARGURA, y)
    y = _pagador(d, y, dados, "Beneficiário Final:")

    c.setLineWidth(1.6)
    c.line(x0, y - 2 * mm, x0 + LARGURA, y - 2 * mm)

    # código de barras ITF 2 de 5 intercalado (FEBRABAN: barra fina 0,254 mm, razão 3, altura 13 mm)
    barras = I2of5(codigo_barras, barWidth=0.254 * mm, ratio=3, barHeight=13 * mm,
                   checksum=0, bearers=0, quiet=0, humanReadable=0)
    barras.drawOn(c, x0, y - 2 * mm - 15 * mm)
    c.setFont(FONTE, 6.5)
    c.drawString(x0 + COL_ESQUERDA + 1.5 * mm, y - 5.5 * mm, "Autenticação Mecânica")

    if teste:
        _marca_teste(c)

    c.showPage()
    c.save()

    return str(arquivo), teste
