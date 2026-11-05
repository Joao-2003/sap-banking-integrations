"""Arquivos fictícios da demonstração do app de remessas.

No modo demonstração o app chama gerar() a cada sessão (datas sempre relativas a hoje);
rodando este arquivo, eles são gravados em demo/.

- remessa_sap_exemplo.txt: remessa CNAB 240 como o SAP gera (segmentos A + B para todos os pagamentos);
- dda_sap.json: o que a consulta YY1_DDA devolveria (boletos DDA registrados contra a empresa).

Empresas, CNPJs, contas e boletos são inventados; os dígitos verificadores seguem as regras da
FEBRABAN para o arquivo passar pelas mesmas validações do arquivo real. Semente fixa.

    python remessas/gerar_exemplo.py
"""
import json
import os
import random
from datetime import date, timedelta

AQUI = os.path.dirname(os.path.abspath(__file__))
DEMO = os.path.join(AQUI, "demo")
rnd = random.Random(240)
HOJE = date.today()
BASE_FATOR = date(2025, 2, 22)  # fator de vencimento reiniciado em 1000 (FEBRABAN)


def dv_cnpj(base12):
    def dv(nums, pesos):
        s = sum(int(n) * p for n, p in zip(nums, pesos)) % 11
        return "0" if s < 2 else str(11 - s)
    d1 = dv(base12, [5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2])
    d2 = dv(base12 + d1, [6, 5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2])
    return base12 + d1 + d2


def mod10(num):
    total, peso = 0, 2
    for d in reversed(num):
        p = int(d) * peso
        total += p // 10 + p % 10
        peso = 1 if peso == 2 else 2
    return str((10 - total % 10) % 10)


def mod11_barras(num):
    total, peso = 0, 2
    for d in reversed(num):
        total += int(d) * peso
        peso = 2 if peso == 9 else peso + 1
    r = 11 - total % 11
    return "1" if r in (0, 1, 10, 11) else str(r)


def boleto(banco, venc, valor_centavos):
    fator = str(1000 + (venc - BASE_FATOR).days).zfill(4)
    livre = "".join(rnd.choice("0123456789") for _ in range(25))
    sem_dv = f"{banco}9" + fator + str(valor_centavos).zfill(10) + livre
    barras = sem_dv[:4] + mod11_barras(sem_dv) + sem_dv[4:]
    c1 = barras[0:4] + barras[19:24]
    c2 = barras[24:34]
    c3 = barras[34:44]
    linha = c1 + mod10(c1) + c2 + mod10(c2) + c3 + mod10(c3) + barras[4] + barras[5:19]
    return linha, barras


def campo(v, n, num=False):
    v = str(v)
    return v.zfill(n)[:n] if num else v.ljust(n)[:n]


EMPRESA = ("EMPRESA EXEMPLO INDUSTRIA LTDA", dv_cnpj("112223330001"))
BANCO_EMPRESA = "341"
FORNECEDORES = [
    # nome, tem DDA?, ajuste do DDA (dias, centavos)
    ("ACOS PLANALTO LTDA", True, (0, 0)), ("BORRACHAS DO VALE LTDA", True, (0, 0)),
    ("TINTAS CERRADO EIRELI", True, (0, 37)), ("PARAFUSOS UNIAO LTDA", True, (-3, 0)),
    ("HIDRAULICA CENTRO OESTE", True, (0, 0)), ("ELETRONICA MODELO LTDA", True, (-5, 80)),
    ("EMBALAGENS EXEMPLO S.A.", True, (0, 0)), ("ROLAMENTOS FICTICIOS LTDA", True, (0, 0)),
    ("SERVICOS DE LIMPEZA ALFA", False, None), ("CONSULTORIA BETA ME", False, None),
    ("MANUTENCAO GAMA LTDA", False, None), ("TRANSPORTES DELTA LTDA", False, None),
    ("USINAGEM OMEGA LTDA", True, (0, 250)),  # diferença acima de R$ 1,00: fica bloqueado
]


def gerar(hoje=None):
    """Devolve (bytes da remessa CNAB 240, lista do DDA) com datas relativas a hoje."""
    global HOJE
    HOJE = hoje or date.today()
    rnd.seed(240)
    venc = HOJE + timedelta(days=1)
    linhas = []
    linhas.append(campo(BANCO_EMPRESA, 3, True) + "0000" + "0" + " " * 9 + "2" + EMPRESA[1] + campo("CONVENIOEX01", 20)
                  + "01234" + " " + campo("56789", 12, True) + " " + "0" + campo(EMPRESA[0], 30) + campo("BANCO EXEMPLO", 30)
                  + " " * 10 + "1" + HOJE.strftime("%d%m%Y") + "093000" + "000001" + "089" + "00000" + " " * 69)
    linhas.append(campo(BANCO_EMPRESA, 3, True) + "0001" + "1" + "C" + "20" + "01" + "045" + " " + "2" + EMPRESA[1]
                  + campo("CONVENIOEX01", 20) + "01234" + " " + campo("56789", 12, True) + " " + "0" + campo(EMPRESA[0], 30)
                  + " " * 40 + campo("RUA DAS INDUSTRIAS", 30) + "01000" + " " * 15 + campo("ANAPOLIS", 20) + "75000000" + "GO"
                  + " " * 8 + " " * 10)
    dda, seq, total = [], 1, 0
    for i, (nome, tem_dda, ajuste) in enumerate(FORNECEDORES):
        cnpj = dv_cnpj(f"{20000000 + i * 1111:08d}0001")
        valor = rnd.randint(35000, 2500000)
        total += valor
        banco_fav = rnd.choice(["001", "237", "104", "033", "341", "756"])
        ag, conta = f"{rnd.randint(100, 9999):05d}", f"{rnd.randint(10000, 999999):012d}"
        seg_a = (campo(BANCO_EMPRESA, 3, True) + "0001" + "3" + campo(seq, 5, True) + "A" + "0" + "00" + "018" + banco_fav
                 + ag + str(rnd.randint(0, 9)) + conta + str(rnd.randint(0, 9)) + " " + campo(nome, 30)
                 + campo(f"NF{rnd.randint(1000, 99999)}", 20) + venc.strftime("%d%m%Y") + "BRL" + "0" * 15
                 + campo(valor, 15, True) + " " * 20 + "0" * 8 + "0" * 15 + " " * 40 + "00" + " " * 5 + "  " + " " * 3 + "0" + " " * 10)
        seq += 1
        seg_b = (campo(BANCO_EMPRESA, 3, True) + "0001" + "3" + campo(seq, 5, True) + "B" + "   " + "2" + cnpj
                 + campo("AV EXEMPLO", 30) + campo(rnd.randint(1, 999), 5, True) + " " * 15 + campo("BAIRRO", 15)
                 + campo("GOIANIA", 20) + "74000000" + "GO" + venc.strftime("%d%m%Y") + "0" * 75 + " " * 15 + " " * 8)
        seq += 1
        linhas += [seg_a, seg_b]
        if tem_dda:
            dias, dif = ajuste
            v_dda, d_dda = valor + dif, venc + timedelta(days=dias)
            ld, _ = boleto(rnd.choice(["237", "341", "001"]), d_dda, v_dda)
            dda.append({"MemoLine": f"{ld} DDA BARCODE {cnpj};{d_dda.strftime('%d%m%Y')};{v_dda}"})
    # um boleto DDA vencendo hoje que ficou fora da remessa (o app avisa na auditoria de caixa)
    ld, _ = boleto("104", HOJE, 189900)
    dda.append({"MemoLine": f"{ld} DDA BARCODE {dv_cnpj('309876540001')};{HOJE.strftime('%d%m%Y')};189900"})
    qtd_lote = len(FORNECEDORES) * 2 + 2
    linhas.append(campo(BANCO_EMPRESA, 3, True) + "0001" + "5" + " " * 9 + campo(qtd_lote, 6, True) + campo(total, 18, True)
                  + "0" * 18 + "0" * 6 + " " * 165 + " " * 10)
    linhas.append(campo(BANCO_EMPRESA, 3, True) + "9999" + "9" + " " * 9 + "000001" + campo(qtd_lote + 2, 6, True)
                  + "000000" + " " * 205)
    linhas = [l.ljust(240)[:240] for l in linhas]
    return ("\r\n".join(linhas) + "\r\n").encode("latin-1"), dda


def main():
    os.makedirs(DEMO, exist_ok=True)
    rem, dda = gerar()
    with open(os.path.join(DEMO, "remessa_sap_exemplo.txt"), "wb") as fh:
        fh.write(rem)
    with open(os.path.join(DEMO, "dda_sap.json"), "w", encoding="utf-8") as fh:
        json.dump(dda, fh, ensure_ascii=False, indent=1)
    print(f"{len(FORNECEDORES)} pagamentos na remessa · {len(dda)} boletos no DDA")


if __name__ == "__main__":
    main()
