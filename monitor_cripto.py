#!/usr/bin/env python3
"""
monitor_cripto.py — Monitor de carteira na Binance com detecção de
suporte/resistência e alertas (console, log e Telegram).

O programa NÃO faz previsão nem recomendação de investimento: ele identifica
padrões técnicos conhecidos (zonas de suporte/resistência, pressão sobre a
zona, compressão de volatilidade, faixa lateral, rompimento, reteste/acomodação
e falso rompimento) e avisa quando eles aparecem.

Projeto: monitor-cripto (licença MIT). Configuração em config.json e .env.

Uso:
  python3 monitor_cripto.py            # roda em loop contínuo
  python3 monitor_cripto.py --once     # um ciclo e sai (bom para testar)
  python3 monitor_cripto.py --demo     # simulação offline, sem Binance
"""
import argparse
import hashlib
import hmac
import json
import logging
import math
import os
import random
import re
import sys
import queue
import threading
import time
from logging.handlers import RotatingFileHandler
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlencode

try:
    import requests
except ImportError:  # pragma: no cover
    sys.exit("Falta a dependência 'requests': pip install requests")

BASE_URL = "https://api.binance.com"
FAPI_URL = "https://fapi.binance.com"   # Futuros USDⓈ-M
APP_DIR = Path(__file__).resolve().parent
STABLES = {"USDT", "USDC", "FDUSD", "BUSD", "TUSD", "DAI"}
log = logging.getLogger("monitor")

DEFAULT_CONFIG = {
    "moeda_cotacao": "USDT",
    "usar_carteira": True,            # lê saldos da conta (chave SOMENTE LEITURA)
    "simbolos_extras": ["BTCUSDT"],   # pares monitorados mesmo sem saldo
    "valor_minimo_cotacao": 10,       # ignora saldos menores que isso (poeira)
    "intervalos": ["1h", "4h"],
    "velas": 500,
    "intervalo_verificacao_s": 60,
    "pivo_janela": 5,                 # velas de cada lado para um topo/fundo valer
    "min_toques": 2,                  # toques mínimos para uma zona valer
    "tolerancia_zona_atr": 0.6,       # pivôs a menos de X ATR viram a mesma zona
    "proximidade_atr": 0.5,           # "perto da zona" = a menos de X ATR
    "rompimento_atr": 0.25,           # fechamento X ATR além da zona = rompimento
    "volume_rompimento": 1.3,         # volume da vela ≥ X × média = confirmado
    "faixa_min_atr": 2.0,
    "faixa_max_atr": 10.0,
    "compressao_limite": 0.75,        # ATR curto/longo abaixo disso = compressão
    "validade_rompimento_velas": 30,  # por quantas velas acompanhar o reteste
    "variacao_carteira_pct": 3.0,     # alerta se o total da carteira mudar X%
    "cooldown_min": 240,              # não repete o mesmo alerta antes disso
    "monitorar_ordens": True,         # ordens abertas no Spot (e Futuros, se houver)
    "monitorar_futuros": True,        # desliga sozinho se a chave não tiver acesso
    "intervalo_ordens_s": 15,         # checagem de execução de ordens
    "relatorio_ordens_min": 5,        # relatório enquanto houver ordem/posição aberta
    # Agenda (valores iniciais; depois ajuste pelo Telegram com /agenda e /frequencia)
    "agenda_padrao": "24/7",          # ou "uteis 08:00-18:00", "seg,qua,sex 09:00-17:00"
    "ordens_fora_do_horario": True,   # avisos de execução chegam mesmo fora do horário
    "freq_analise_min": 720,          # 📊 análise periódica (0 desliga)
    "freq_sugestao_min": 0,           # 📈 /tendencia automático (0 desliga)
    "freq_regime_min": 0,             # 🌡️ /regime automático (0 = só na mudança)
    "sugestao_simbolos": [],          # pares da sugestão automática (vazio = simbolos_extras)
    "comandos_telegram": True,        # o próprio monitor lê /status, /pausar, /retomar
                                      # (use false se o bot já é lido por outro programa)
    "pasta_comandos": "",             # alternativa: outro programa grava *.cmd aqui;
                                      # vazio = ./comandos
    "regime": True,                   # camada diária: on-chain, sentimento, notícias, macro
    "regime_horas": 6,                # recálculo do regime (os dados mudam 1x/dia)
    "regime_peso_tendencia": 0.20,    # peso do regime dentro do /tendencia
    "noticias_min": 30,               # leitura dos feeds RSS + classificação
    "ollama_url": "http://127.0.0.1:11434",
    "ollama_modelo": "qwen2.5:3b",
    "feeds_rss": [
        "https://www.coindesk.com/arc/outboundfeeds/rss/",
        "https://cointelegraph.com/rss",
        "https://decrypt.co/feed",
        "https://bitcoinmagazine.com/.rss/full/",
        "https://www.theblock.co/rss.xml",
    ],
}

COMANDOS_MENU = [
    {"command": "status", "description": "Análise atual e posições abertas"},
    {"command": "pausar", "description": "Pausa os alertas (ex.: /pausar 2h)"},
    {"command": "retomar", "description": "Retoma os alertas"},
    {"command": "tendencia", "description": "Sugestão de direção e alvos (ex.: /tendencia ETH)"},
    {"command": "regime", "description": "Termômetro diário: on-chain, sentimento, macro"},
    {"command": "config", "description": "Ver e alterar a configuração com botões"},
    {"command": "agenda", "description": "Horário de atividade (ex.: /agenda uteis 08:00-18:00)"},
    {"command": "frequencia", "description": "Frequência dos relatórios (ex.: /frequencia regime 2h)"},
    {"command": "ajuda", "description": "O que significa cada informação"},
]

# Avisos de ordens guardados durante a pausa e entregues ao retomar
CRITICOS = ("✅", "❌", "🟡", "🎯", "🏁", "🆕", "⚠️")
ORDENS_URGENTES = ("✅", "❌", "🟡", "🎯", "🏁", "🆕")


# --------------------------------------------------------------------------
# Configuração e ambiente
# --------------------------------------------------------------------------
def load_env(path: Path):
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def load_config(path: Path) -> dict:
    cfg = dict(DEFAULT_CONFIG)
    if path.exists():
        cfg.update(json.loads(path.read_text()))
    return cfg


def load_state(path: Path) -> dict:
    if path.exists():
        try:
            return json.loads(path.read_text())
        except json.JSONDecodeError:
            log.warning("estado.json corrompido; recomeçando do zero")
    return {}


def save_state(path: Path, state: dict):
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=1))
    tmp.replace(path)


# --------------------------------------------------------------------------
# Cliente Binance (somente leitura)
# --------------------------------------------------------------------------
@dataclass
class Candle:
    t: int      # abertura (ms)
    o: float
    h: float
    l: float
    c: float
    v: float
    ct: int     # fechamento (ms)
    tb: float = 0.0   # volume de compras agressivas (taker buy)


class BinanceErro(Exception):
    pass


DICAS_ERRO = {
    -2015: "chave inválida, IP fora da restrição ou chave sem 'Enable Reading'",
    -2014: "formato da chave inválido (colagem incompleta?)",
    -1022: "assinatura inválida: confira o BINANCE_API_SECRET",
    -1021: "relógio do servidor fora de sincronia (rode: timedatectl)",
}


class Binance:
    def __init__(self, key=None, secret=None):
        self.s = requests.Session()
        self.key, self.secret = key, secret
        if key:
            self.s.headers["X-MBX-APIKEY"] = key

    def get(self, path, params=None, signed=False, base=BASE_URL):
        for tentativa in range(4):
            p = dict(params or {})
            if signed:
                p["timestamp"] = int(time.time() * 1000)
                p["recvWindow"] = 10000
                q = urlencode(p)
                p["signature"] = hmac.new(self.secret.encode(), q.encode(),
                                          hashlib.sha256).hexdigest()
            try:
                r = self.s.get(base + path, params=p, timeout=15)
                if r.status_code in (418, 429):
                    espera = int(r.headers.get("Retry-After", 60))
                    log.warning("Limite da API atingido; aguardando %ss", espera)
                    time.sleep(espera)
                    continue
                if 400 <= r.status_code < 500:
                    # erro do cliente (chave, IP, assinatura): não adianta repetir
                    try:
                        d = r.json()
                        msg = f"Binance {r.status_code} código {d.get('code')}: {d.get('msg')}"
                    except ValueError:
                        msg = f"Binance {r.status_code}: {r.text[:200]}"
                    raise BinanceErro(msg)
                r.raise_for_status()
                return r.json()
            except requests.RequestException as e:
                if tentativa == 3:
                    raise
                log.warning("Erro na API (%s); nova tentativa", e)
                time.sleep(3 * (tentativa + 1))

    def valid_symbols(self, quote):
        info = self.get("/api/v3/exchangeInfo", {"permissions": "SPOT"})
        return {s["symbol"] for s in info["symbols"]
                if s["status"] == "TRADING" and s["quoteAsset"] == quote}

    def prices(self):
        return {d["symbol"]: float(d["price"]) for d in self.get("/api/v3/ticker/price")}

    def change24(self, symbols):
        if not symbols:
            return {}
        data = self.get("/api/v3/ticker/24hr", {
            "symbols": json.dumps(sorted(symbols), separators=(",", ":"))})
        return {d["symbol"]: float(d["priceChangePercent"]) for d in data}

    def klines(self, symbol, interval, limit):
        raw = self.get("/api/v3/klines",
                       {"symbol": symbol, "interval": interval, "limit": limit})
        cs = [Candle(int(k[0]), float(k[1]), float(k[2]), float(k[3]),
                     float(k[4]), float(k[5]), int(k[6]),
                     float(k[9]) if len(k) > 9 else 0.0) for k in raw]
        agora = int(time.time() * 1000)
        if cs and cs[-1].ct > agora:   # vela ainda em formação: fora da análise
            cs.pop()
        return cs

    def balances(self):
        acc = self.get("/api/v3/account", {"omitZeroBalances": "true"}, signed=True)
        out = {}
        for b in acc["balances"]:
            q = float(b["free"]) + float(b["locked"])
            if q > 0:
                out[b["asset"]] = out.get(b["asset"], 0) + q
        return out


# --------------------------------------------------------------------------
# Indicadores
# --------------------------------------------------------------------------
def ema(vals, n):
    k, out, e = 2 / (n + 1), [], vals[0]
    for v in vals:
        e = v * k + e * (1 - k)
        out.append(e)
    return out


def true_ranges(cs):
    tr = [cs[0].h - cs[0].l]
    for i in range(1, len(cs)):
        p = cs[i - 1].c
        tr.append(max(cs[i].h - cs[i].l, abs(cs[i].h - p), abs(cs[i].l - p)))
    return tr


def wilder(vals, n):
    out, a = [], sum(vals[:n]) / n
    for i, v in enumerate(vals):
        if i >= n:
            a = (a * (n - 1) + v) / n
        out.append(a)
    return out


def rsi(closes, n=14):
    g = [0.0] + [max(closes[i] - closes[i - 1], 0) for i in range(1, len(closes))]
    p = [0.0] + [max(closes[i - 1] - closes[i], 0) for i in range(1, len(closes))]
    ag, ap = wilder(g, n), wilder(p, n)
    return [100.0 if b == 0 else 100 - 100 / (1 + a / b) for a, b in zip(ag, ap)]


def slope(vals):
    """Inclinação da regressão linear (por vela)."""
    n = len(vals)
    mx, my = (n - 1) / 2, sum(vals) / n
    num = sum((i - mx) * (v - my) for i, v in enumerate(vals))
    den = sum((i - mx) ** 2 for i in range(n)) or 1
    return num / den


def mean(x):
    return sum(x) / len(x) if x else 0.0


# --------------------------------------------------------------------------
# Zonas de suporte e resistência
# --------------------------------------------------------------------------
@dataclass
class Zona:
    low: float
    high: float
    toques: int
    ultimo: int                       # índice da vela do toque mais recente
    tipos: set = field(default_factory=set)   # 'R' (topos) e/ou 'S' (fundos)

    @property
    def mid(self):
        return (self.low + self.high) / 2


def pivots(cs, k):
    topos, fundos = [], []
    for i in range(k, len(cs) - k):
        jan = cs[i - k:i + k + 1]
        if cs[i].h == max(c.h for c in jan):
            topos.append((i, cs[i].h))
        if cs[i].l == min(c.l for c in jan):
            fundos.append((i, cs[i].l))
    return topos, fundos


def build_zones(topos, fundos, atr, cfg):
    pts = sorted([(p, i, "R") for i, p in topos] + [(p, i, "S") for i, p in fundos])
    tol = cfg["tolerancia_zona_atr"] * atr
    clusters, atual = [], []
    for pt in pts:
        if atual and pt[0] - mean([x[0] for x in atual]) > tol:
            clusters.append(atual)
            atual = []
        atual.append(pt)
    if atual:
        clusters.append(atual)
    pad = 0.15 * atr
    zonas = []
    for cl in clusters:
        if len(cl) < cfg["min_toques"]:
            continue
        precos = [x[0] for x in cl]
        zonas.append(Zona(min(precos) - pad, max(precos) + pad, len(cl),
                          max(x[1] for x in cl), {x[2] for x in cl}))
    return zonas


# --------------------------------------------------------------------------
# Análise de um par/intervalo
# --------------------------------------------------------------------------
def fmt(p):
    if p >= 1000:
        return f"{p:,.0f}".replace(",", ".")
    if p >= 1:
        return f"{p:.2f}".replace(".", ",")
    casas = max(4, -int(math.floor(math.log10(p))) + 3)
    return f"{p:.{casas}f}".replace(".", ",")


def zid(preco):
    """Identificador estável de zona (grade de ~0,5%), imune a pequenas mudanças de borda."""
    return str(round(math.log(preco) / 0.005))


def zona_txt(z):
    return f"{fmt(z.low)}–{fmt(z.high)} ({z.toques} toques)"


@dataclass
class Sinal:
    chave: str
    titulo: str
    linhas: list


def analyze(cs, price, cfg, st):
    """Retorna (resumo, [Sinal]). `st` é o estado persistente deste par/intervalo."""
    if len(cs) < 80:
        return None, []
    C = [c.c for c in cs]
    V = [c.v for c in cs]
    tr = true_ranges(cs)
    A = wilder(tr, 14)[-1]
    if A <= 0:
        return None, []
    comp = mean(tr[-5:]) / (mean(tr[-30:]) or A)
    e20, e50 = ema(C, 20), ema(C, 50)
    R = rsi(C, 14)
    incl = (e50[-1] - e50[-11]) / A
    if C[-1] > e20[-1] > e50[-1] and incl > 0.3:
        tendencia = "alta"
    elif C[-1] < e20[-1] < e50[-1] and incl < -0.3:
        tendencia = "baixa"
    else:
        tendencia = "lateral"

    topos, fundos = pivots(cs, cfg["pivo_janela"])
    zonas = build_zones(topos, fundos, A, cfg)
    res = min((z for z in zonas if z.mid > price), key=lambda z: z.mid, default=None)
    sup = max((z for z in zonas if z.mid <= price), key=lambda z: z.mid, default=None)
    vol_med = mean(V[-21:-1]) or 1
    vol_rec = mean(V[-5:]) / vol_med
    prox = cfg["proximidade_atr"]
    base = [f"Preço: {fmt(price)} | tendência: {tendencia} | RSI {R[-1]:.0f}"]
    sinais = []

    resumo = {"tendencia": tendencia, "rsi": round(R[-1]),
              "suporte": zona_txt(sup) if sup else "—",
              "resistencia": zona_txt(res) if res else "—"}

    # 1) Pressão sobre a resistência --------------------------------------
    if res and (res.low - price) / A <= prox and price <= res.high + 0.1 * A:
        testes = [c for c in cs[-12:] if c.h >= res.low - 0.1 * A]
        if len(testes) >= 2:
            pro, contra = [], []
            if comp < cfg["compressao_limite"]:
                pro.append("volatilidade comprimida")
            if slope([c.l for c in cs[-12:]]) / A > 0.03:
                pro.append("fundos ascendentes (compressão contra a zona)")
            if vol_rec > 1.2:
                pro.append("volume crescente")
            if tendencia == "alta":
                pro.append("tendência de alta")
            pavios = sum(1 for c in testes if c.h > c.l and
                         (c.h - max(c.o, c.c)) / (c.h - c.l) > 0.5)
            if pavios >= 2:
                contra.append(f"{pavios} pavios de rejeição")
            if R[-1] > 70:
                contra.append("RSI sobrecomprado")
            if tendencia == "baixa":
                contra.append("tendência de baixa")
            if cs[-1].h >= cs[res.ultimo].h - 0.1 * A and res.ultimo < len(cs) - 3 \
                    and R[-1] < R[res.ultimo] - 5:
                contra.append("divergência de RSI (força menor no novo teste)")
            if len(pro) >= 3 and len(pro) > len(contra):
                leitura = "rompimento para cima parece iminente"
            elif len(contra) > len(pro):
                leitura = "risco de rejeição — possível volta para dentro da faixa"
            else:
                leitura = "indefinido — esperar o fechamento confirmar"
            sinais.append(Sinal(
                f"pressaoR:{zid(res.mid)}:{leitura[:10]}",
                "Pressão na resistência",
                base + [f"Resistência: {zona_txt(res)} — {len(testes)} testes nas últimas 12 velas",
                        "A favor do rompimento: " + (", ".join(pro) or "nada"),
                        "Contra: " + (", ".join(contra) or "nada"),
                        f"Leitura: {leitura}"]))

    # 2) Teste de suporte ---------------------------------------------------
    if sup and (price - sup.high) / A <= prox and price >= sup.low - 0.1 * A:
        testes = [c for c in cs[-12:] if c.l <= sup.high + 0.1 * A]
        if len(testes) >= 2:
            queda, segura = [], []
            if comp < cfg["compressao_limite"]:
                queda.append("volatilidade comprimida")
            if slope([c.h for c in cs[-12:]]) / A < -0.03:
                queda.append("topos descendentes (pressão sobre o suporte)")
            if vol_rec > 1.2 and C[-1] < C[-6]:
                queda.append("volume crescente na descida")
            if tendencia == "baixa":
                queda.append("tendência de baixa")
            pavios = sum(1 for c in testes if c.h > c.l and
                         (min(c.o, c.c) - c.l) / (c.h - c.l) > 0.5)
            if pavios >= 2:
                segura.append(f"{pavios} pavios de defesa (compradores)")
            if R[-1] < 30:
                segura.append("RSI sobrevendido")
            if tendencia == "alta":
                segura.append("tendência de alta")
            if cs[-1].l <= cs[sup.ultimo].l + 0.1 * A and sup.ultimo < len(cs) - 3 \
                    and R[-1] > R[sup.ultimo] + 5:
                segura.append("divergência de RSI (venda perdendo força)")
            if len(queda) >= 3 and len(queda) > len(segura):
                leitura = "rompimento para baixo parece iminente"
            elif len(segura) > len(queda):
                leitura = "suporte segurando — possível repique"
            else:
                leitura = "indefinido — esperar o fechamento confirmar"
            sinais.append(Sinal(
                f"testeS:{zid(sup.mid)}:{leitura[:10]}",
                "Teste de suporte",
                base + [f"Suporte: {zona_txt(sup)} — {len(testes)} testes nas últimas 12 velas",
                        "A favor da perda do suporte: " + (", ".join(queda) or "nada"),
                        "A favor de segurar: " + (", ".join(segura) or "nada"),
                        f"Leitura: {leitura}"]))

    # 3) Faixa estável entre suporte e resistência --------------------------
    if res and sup:
        largura = (res.low - sup.high) / A
        if cfg["faixa_min_atr"] <= largura <= cfg["faixa_max_atr"] and all(
                sup.low - 0.3 * A <= c.c <= res.high + 0.3 * A for c in cs[-20:]):
            pos = (price - sup.high) / (res.low - sup.high) * 100
            pos = max(0, min(100, pos))
            meio = (sup.high + res.low) / 2
            sinais.append(Sinal(
                f"faixa:{zid(sup.mid)}-{zid(res.mid)}",
                "Faixa lateral (zona estável)",
                base + [f"Preço oscilando entre {fmt(sup.high)} e {fmt(res.low)} há 20+ velas",
                        f"Meio da faixa: {fmt(meio)} | posição atual: {pos:.0f}% da faixa",
                        "Perto do piso = região de suporte; perto do teto = região de resistência"]))

    # 4) Rompimentos na última vela fechada ----------------------------------
    st.setdefault("rompimentos", {})
    ult, ant = cs[-1], cs[-2]
    b = cfg["rompimento_atr"] * A
    vol_ult = V[-1] / vol_med
    for z in zonas:
        direcao = None
        # só a PRIMEIRA vela que fecha além da zona conta como rompimento
        # e o preço precisa vir do outro lado da zona (senão é só um repique nela)
        antes = cs[-11:-1]
        if ult.c > z.high + b and ant.c <= z.high + b \
                and sum(c.c < z.mid for c in antes) >= 6:
            direcao = "alta"
        elif ult.c < z.low - b and ant.c >= z.low - b \
                and sum(c.c > z.mid for c in antes) >= 6:
            direcao = "baixa"
        if not direcao:
            continue
        # mesma zona (bordas que se sobrepõem) já registrada nesse sentido? ignora
        if any(bo["dir"] == direcao and bo["low"] <= z.high and z.low <= bo["high"]
               for bo in st["rompimentos"].values()):
            continue
        chave = f"{direcao}:{zid(z.mid)}:{ult.t}"
        conf = vol_ult >= cfg["volume_rompimento"]
        st["rompimentos"][chave] = {"dir": direcao, "low": z.low, "high": z.high,
                                    "t": ult.t, "acomodou": False}
        if direcao == "alta":
            prox_z = min((y for y in zonas if y.low > ult.c), key=lambda y: y.low, default=None)
            alvo = f"Próxima resistência: {zona_txt(prox_z)}" if prox_z else "Sem resistência mapeada acima"
            papel = "antiga resistência tende a virar suporte"
        else:
            prox_z = max((y for y in zonas if y.high < ult.c), key=lambda y: y.high, default=None)
            alvo = f"Próximo suporte: {zona_txt(prox_z)}" if prox_z else "Sem suporte mapeado abaixo"
            papel = "antigo suporte tende a virar resistência"
        sinais.append(Sinal(
            f"romp:{chave}",
            f"Rompimento de {direcao} {'CONFIRMADO por volume' if conf else '(volume fraco)'}",
            base + [f"Fechou fora da zona {zona_txt(z)} — volume {vol_ult:.1f}× a média",
                    f"Zona provável de acomodação (reteste): {fmt(z.low)}–{fmt(z.high)} — {papel}",
                    alvo]))

    # 5) Acompanhamento: reteste/acomodação ou falso rompimento --------------
    validade = cfg["validade_rompimento_velas"]
    for chave, bo in list(st["rompimentos"].items()):
        depois = [c for c in cs if c.t > bo["t"]]
        if len(depois) > validade or (not depois and cs[0].t > bo["t"]):
            del st["rompimentos"][chave]
            continue
        if not depois:
            continue
        lc = depois[-1]
        alta = bo["dir"] == "alta"
        if (alta and lc.c < bo["low"] - 0.1 * A) or (not alta and lc.c > bo["high"] + 0.1 * A):
            sinais.append(Sinal(
                f"falso:{chave}", f"Falso rompimento de {bo['dir']}",
                base + [f"Preço voltou para dentro/abaixo da zona {fmt(bo['low'])}–{fmt(bo['high'])}",
                        "O rompimento não se sustentou; a zona volta a valer como antes"]))
            del st["rompimentos"][chave]
            continue
        if bo["acomodou"]:
            continue
        if alta:
            tocou = any(c.l <= bo["high"] + 0.3 * A for c in depois)
            segurou = lc.c > bo["high"] and lc.c > lc.o
        else:
            tocou = any(c.h >= bo["low"] - 0.3 * A for c in depois)
            segurou = lc.c < bo["low"] and lc.c < lc.o
        if tocou and segurou:
            bo["acomodou"] = True
            sinais.append(Sinal(
                f"acomod:{chave}", f"Acomodação após rompimento de {bo['dir']}",
                base + [f"Preço retestou {fmt(bo['low'])}–{fmt(bo['high'])} e "
                        f"{'segurou acima' if alta else 'foi rejeitado abaixo'}",
                        "Nova posição de acomodação: a zona rompida passou a funcionar "
                        f"como {'suporte' if alta else 'resistência'}"]))

    return resumo, sinais


# --------------------------------------------------------------------------
# Alertas
# --------------------------------------------------------------------------

# --------------------------------------------------------------------------
# Agenda: horário geral, horário próprio por item e frequência dos relatórios
# (ajustável pelo Telegram: /config com botões, ou /agenda e /frequencia)
# --------------------------------------------------------------------------
DIAS = ["seg", "ter", "qua", "qui", "sex", "sab", "dom"]
# item: (descrição, é periódico?)
ITENS = {
    "analise": ("📊 Análise periódica", True),
    "sugestao": ("📈 Sugestão de tendência", True),
    "regime": ("🌡️ Regime periódico", True),
    "ordens": ("📋 Relatório de ordens abertas", True),
    "padrao": ("🔔 Alertas de padrão", False),
    "carteira": ("💼 Variação da carteira", False),
    "avisos_ordens": ("🧾 Execução de ordens", False),
    "regime_mudanca": ("🌡️ Aviso de mudança de regime", False),
}
TIPOS_FREQ = {k: v[0] for k, v in ITENS.items() if v[1]}
SINONIMOS_FREQ = {"análise": "analise", "resumo": "analise", "sugestão": "sugestao",
                  "tendencia": "sugestao", "tendência": "sugestao", "ordem": "ordens",
                  "posicoes": "ordens", "posições": "ordens"}
OPCOES_FREQ = [5, 15, 30, 60, 120, 240, 360, 720, 1440]


def _hm(txt):
    h, _, m = txt.strip().rstrip("h").partition(":")
    h, m = int(h), int(m or 0)
    if not (0 <= h <= 24 and 0 <= m < 60) or h * 60 + m > 1440:
        raise ValueError
    return h * 60 + m


def _hm_txt(m):
    return f"{m // 60:02d}:{m % 60:02d}"


def _dur_txt(minutos):
    if not minutos:
        return "desligado"
    if minutos % 1440 == 0:
        return f"a cada {minutos // 1440} dia(s)"
    if minutos % 60 == 0:
        return f"a cada {minutos // 60}h"
    return f"a cada {minutos} min"


def _dentro_j(j, t=None):
    """j: None = sempre; dict = {dias, inicio, fim} em minutos do dia."""
    if not j:
        return True
    lt = time.localtime(t or time.time())
    minuto, dia = lt.tm_hour * 60 + lt.tm_min, lt.tm_wday
    if j["inicio"] <= j["fim"]:
        return dia in j["dias"] and j["inicio"] <= minuto < j["fim"]
    if minuto >= j["inicio"]:                # janela que atravessa a meia-noite
        return dia in j["dias"]
    return minuto < j["fim"] and (dia - 1) % 7 in j["dias"]


def _janela_txt(j):
    if not j:
        return "24/7"
    if j["dias"] == [0, 1, 2, 3, 4]:
        d = "dias úteis"
    elif j["dias"] == list(range(7)):
        d = "todos os dias"
    elif j["dias"] == [5, 6]:
        d = "fim de semana"
    else:
        d = ", ".join(DIAS[x] for x in j["dias"])
    return f"{d}, {_hm_txt(j['inicio'])}–{_hm_txt(j['fim'])}"


class Agenda:
    def __init__(self, cfg, state):
        self.cfg = cfg
        self.st = state.setdefault("agenda", {})
        if "janela" not in self.st:
            self.st["janela"] = self._parse_janela(cfg["agenda_padrao"])
        self.st.setdefault("ordens_sempre", cfg["ordens_fora_do_horario"])
        f = self.st.setdefault("freq", {})
        padrao = {"analise": cfg["freq_analise_min"], "sugestao": cfg["freq_sugestao_min"],
                  "regime": cfg["freq_regime_min"], "ordens": cfg["relatorio_ordens_min"]}
        for k, v in padrao.items():
            f.setdefault(k, v)
        self.st.setdefault("ultimo", {})
        self.st.setdefault("simbolos_sugestao", list(cfg["sugestao_simbolos"]))
        itens = self.st.setdefault("itens", {})
        for k in ITENS:
            # janela: None = usa o horário geral; "24/7" = sempre; dict = própria
            itens.setdefault(k, {"ativo": True, "janela": None})

    # ---- janelas ----------------------------------------------------------
    @staticmethod
    def _parse_janela(txt):
        t = txt.strip().lower()
        if t in ("24/7", "24x7", "24h", "sempre", "continuo", "contínuo", "integral"):
            return None
        # tolera variações: "07:30- 22:00", "7h30 às 22h", "07:30 22:00", "seg - sex"
        t = t.replace("–", "-").replace("—", "-").replace("úteis", "uteis")
        t = re.sub(r"\s+(?:às|as|a|até|ate)\s+", "-", t)
        t = re.sub(r"\s*-\s*", "-", t)
        t = re.sub(r"(\d)h(\d{2})", r"\1:\2", t)
        t = re.sub(r"(\d)h\b", r"\1:00", t)
        partes = t.split()
        hora = re.compile(r"^\d{1,2}(:\d{2})?$")
        if len(partes) >= 2 and hora.match(partes[-1]) and hora.match(partes[-2]):
            partes = partes[:-2] + [partes[-2] + "-" + partes[-1]]
        if len(partes) == 1 and "-" in partes[0] and re.match(r"^\d", partes[0]):
            partes = ["todos"] + partes
        if len(partes) != 2:
            raise ValueError
        dias_txt, horas = partes
        if dias_txt in ("uteis", "util", "útil"):
            dias = [0, 1, 2, 3, 4]
        elif dias_txt in ("todos", "diario", "diário", "todo"):
            dias = list(range(7))
        elif dias_txt in ("fds", "fimdesemana"):
            dias = [5, 6]
        else:
            dias = set()
            for pedaco in dias_txt.replace("á", "a").split(","):
                if "-" in pedaco:
                    a, b = (DIAS.index(x[:3]) for x in pedaco.split("-"))
                    dias |= set(range(a, b + 1)) if a <= b else set(range(a, 7)) | set(range(0, b + 1))
                else:
                    dias.add(DIAS.index(pedaco[:3]))
            dias = sorted(dias)
        ini, _, fim = horas.partition("-")
        return {"dias": dias, "inicio": _hm(ini), "fim": _hm(fim)}

    def janela_de(self, item=None):
        if item is None:
            return self.st["janela"]
        j = self.st["itens"][item]["janela"]
        return self.st["janela"] if j is None else (None if j == "24/7" else j)

    def dentro(self, item=None, t=None):
        return _dentro_j(self.janela_de(item), t)

    def janela_txt(self, item=None):
        if item is not None:
            j = self.st["itens"][item]["janela"]
            if j is None:
                return "horário geral"
            return "24/7" if j == "24/7" else _janela_txt(j)
        return _janela_txt(self.st["janela"]) + (" (sempre ativo)" if not self.st["janela"] else "")

    # ---- itens e frequências ---------------------------------------------
    def ativo(self, item):
        if ITENS[item][1]:
            return self.freq(item) > 0
        return self.st["itens"][item]["ativo"]

    def freq(self, tipo):
        return self.st["freq"].get(tipo, 0)

    def vencido(self, tipo, agora=None):
        """True se o relatório periódico está na hora (e marca como enviado)."""
        agora = agora or time.time()
        f = self.freq(tipo)
        if not f or not self.dentro(tipo, agora):
            return False
        if agora - self.st["ultimo"].get(tipo, 0) < f * 60:
            return False
        self.st["ultimo"][tipo] = agora
        return True

    def item_txt(self, k):
        desc, periodico = ITENS[k]
        if not self.ativo(k):
            return f"{desc}: desligado"
        partes = [self.janela_txt(k)]
        if periodico:
            partes.append(_dur_txt(self.freq(k)))
            if k == "sugestao":
                partes.append(", ".join(self.simbolos_sugestao()))
            if k == "ordens":
                partes.append("só com algo aberto")
        if k == "avisos_ordens" and self.st["ordens_sempre"]:
            partes = ["sempre na hora, mesmo fora do horário"]
        return f"{desc}: " + " | ".join(partes)

    def texto(self):
        linhas = ["🗓️ Agenda do monitor",
                  f"Horário geral: {self.janela_txt()}" +
                  ("" if self.dentro() else "  (agora: FORA do horário)"), ""]
        linhas += ["• " + self.item_txt(k) for k in ITENS]
        linhas += ["", "Para ajustar com botões: /config",
                   "Ou por texto: /agenda uteis 08:00-18:00 | /agenda 24/7 | "
                   "/agenda ordens sempre|guardar | /frequencia regime 2h | "
                   "/frequencia sugestao 1h BTC ETH | /frequencia analise off"]
        return "\n".join(linhas)

    def simbolos_sugestao(self):
        return self.st["simbolos_sugestao"] or list(self.cfg["simbolos_extras"]) or ["BTC"]

    # ---- comandos de texto -----------------------------------------------
    def cmd_agenda(self, arg):
        a = arg.strip().lower()
        if not a:
            return self.texto()
        if a.startswith("ordens"):
            op = a.split()[-1]
            if op not in ("sempre", "guardar"):
                return "Use /agenda ordens sempre  ou  /agenda ordens guardar"
            self.st["ordens_sempre"] = op == "sempre"
            return "✅ Avisos de execução fora do horário: " + \
                ("enviados na hora." if op == "sempre" else "guardados até o horário abrir.")
        try:
            self.st["janela"] = self._parse_janela(a)
        except (ValueError, IndexError):
            return ("Não entendi. Exemplos:\n/agenda 24/7\n/agenda uteis 08:00-18:00\n"
                    "/agenda todos 07:30-22:00\n/agenda seg-sex 09:00-17:30\n"
                    "/agenda seg,qua,sex 09:00-17:00\n/agenda 22:00-06:00\n"
                    "Ou use /config para escolher com botões.")
        return "✅ Horário geral: " + self.janela_txt() + \
            ("" if self.dentro() else "\n(agora está fora do horário; alertas ficam guardados)")

    def cmd_freq(self, arg):
        partes = arg.strip().lower().split()
        if not partes:
            return self.texto()
        tipo = SINONIMOS_FREQ.get(partes[0], partes[0])
        if tipo not in TIPOS_FREQ or len(partes) < 2:
            return ("Use /frequencia <tipo> <tempo>. Tipos: analise, sugestao, regime, ordens.\n"
                    "Tempo: 15m, 30m, 1h, 2h, 1d ou off. Ex.: /frequencia regime 2h")
        val = partes[1]
        if val in ("off", "0", "desligar", "nao", "não"):
            minutos = 0
        else:
            seg = duracao(val)
            if not seg or seg < 60:
                return "Tempo inválido. Use, por exemplo, 5m, 30m, 1h, 2h ou 1d (mínimo 1 min)."
            minutos = seg // 60
        self.st["freq"][tipo] = minutos
        self.st["ultimo"][tipo] = 0                 # envia já no próximo ciclo
        extra = ""
        if tipo == "sugestao" and len(partes) > 2:
            self.st["simbolos_sugestao"] = [p.upper() for p in partes[2:]]
            extra = " — " + ", ".join(self.st["simbolos_sugestao"])
        return f"✅ {TIPOS_FREQ[tipo]}: {_dur_txt(minutos)}{extra}"


def categoria_msg(texto):
    """Classifica uma mensagem pelo início, para aplicar liga/desliga e horário do item."""
    if texto.startswith("🔔"):
        return "padrao"
    if texto.startswith("💼"):
        return "carteira"
    if texto.startswith(ORDENS_URGENTES):
        return "avisos_ordens"
    if texto.startswith("📋"):
        return "ordens"
    if texto.startswith("📊 Análise"):
        return "analise"
    if texto.startswith(("📈 Tendência", "📉 Tendência", "↔️ Tendência")):
        return "sugestao"
    if texto.startswith("🌡️ Mudança"):
        return "regime_mudanca"
    if texto.startswith("🌡️ Regime"):
        return "regime"
    return None


def config_txt(cfg, agenda, cache):
    simbolos = cache.get("simbolos") or cfg["simbolos_extras"]
    linhas = ["⚙️ Configuração do monitor", "", "O que está sendo verificado:",
              f"• Moedas: {', '.join(simbolos)}" +
              (" (carteira + extras)" if cfg["usar_carteira"] else ""),
              f"• Tempos gráficos: {', '.join(cfg['intervalos'])}; "
              f"análise a cada {cfg['intervalo_verificacao_s']} s",
              "• Ordens e posições: " + (f"Spot{' + Futuros' if cfg['monitorar_futuros'] else ''}, "
                                        f"a cada {cfg['intervalo_ordens_s']} s"
                                        if cfg["monitorar_ordens"] else "desligado"),
              "• Regime diário: " + (f"on-chain, Fear & Greed, juros, dólar e notícias "
                                     f"({cfg['ollama_modelo']}); recálculo a cada "
                                     f"{cfg['regime_horas']} h, notícias a cada "
                                     f"{cfg['noticias_min']} min" if cfg["regime"] else "desligado"),
              "", f"🗓️ Horário geral: {agenda.janela_txt()}" +
              ("" if agenda.dentro() else " (agora fora do horário)"),
              "", "Categorias de aviso (estado | horário | frequência):"]
    linhas += ["• " + agenda.item_txt(k) for k in ITENS]
    return "\n".join(linhas)


# --------------------------------------------------------------------------
# /config: assistente com botões (inline keyboard) passo a passo
# --------------------------------------------------------------------------
def _bt(texto, dado):
    return {"text": texto, "callback_data": "cfg|" + dado}


class Configurador:
    def __init__(self, cfg, agenda, notif, cache):
        self.cfg, self.ag, self.notif, self.cache = cfg, agenda, notif, cache

    @property
    def wz(self):
        return self.ag.st.setdefault("assistente", {})

    def abrir(self):
        texto = config_txt(self.cfg, self.ag, self.cache) + "\n\nQuer alterar alguma coisa?"
        teclado = [[_bt("✏️ Sim, alterar", "menu"), _bt("👍 Não, está bom", "fim")]]
        mid = self.notif.enviar_teclado(texto, teclado)
        self.wz.clear()
        self.wz.update({"msg": mid, "item": None, "passo": None, "dados": {}})

    # ---- telas --------------------------------------------------------------
    def tela_menu(self, aviso=""):
        linhas = ([aviso, ""] if aviso else []) + ["O que você quer ajustar?", "",
                                                   f"🗓️ Horário geral: {self.ag.janela_txt()}"]
        linhas += ["• " + self.ag.item_txt(k) for k in ITENS]
        tec = [[_bt("🗓️ Horário geral", "item:geral")]]
        ks = list(ITENS)
        for i in range(0, len(ks), 2):
            tec.append([_bt(ITENS[k][0], f"item:{k}") for k in ks[i:i + 2]])
        tec.append([_bt("🪙 Moedas da sugestão", "item:moedas")])
        tec.append([_bt("✅ Concluir", "fim")])
        return "\n".join(linhas), tec

    def tela(self):
        w, d = self.wz, self.wz["dados"]
        item, passo = w["item"], w["passo"]
        nome = "🗓️ Horário geral" if item == "geral" else \
            "🪙 Moedas da sugestão" if item == "moedas" else ITENS[item][0]
        rodape = [_bt("⬅️ Voltar ao menu", "menu"), _bt("❌ Cancelar", "fim")]
        if passo == "ativo":
            return (f"{nome}\n\nPasso 1: ligado ou desligado?",
                    [[_bt("🟢 Ligado", "ativo:1"), _bt("⚪ Desligado", "ativo:0")], rodape])
        if passo == "dias":
            sel = d.setdefault("dias", list(range(7)))
            tec = [[_bt(("✅ " if i in sel else "▫️ ") + DIAS[i], f"dia:{i}") for i in range(r, min(r + 4, 7))]
                   for r in (0, 4)]
            tec.append([_bt("Todos", "pre:todos"), _bt("Dias úteis", "pre:uteis"),
                        _bt("Fim de semana", "pre:fds")])
            extra = [_bt("♾️ 24/7", "sempre")]
            if item != "geral":
                extra.insert(0, _bt("🕘 Usar horário geral", "geral"))
            tec.append(extra)
            tec.append([_bt("Próximo ➡️", "prox")])
            tec.append(rodape)
            return (f"{nome}\n\nPasso {self._n('dias')}: em quais dias da semana?\n"
                    "Toque para marcar ou desmarcar.", tec)
        if passo in ("ini_h", "fim_h"):
            ini = passo == "ini_h"
            horas = range(0, 24) if ini else range(1, 25)
            hs = list(horas)
            tec = [[_bt(f"{h:02d}", f"h:{h}") for h in hs[i:i + 6]] for i in range(0, len(hs), 6)]
            tec.append(rodape)
            return (f"{nome}\n\nPasso {self._n(passo)}: " +
                    ("a partir de que hora?" if ini else
                     f"até que hora? (início {_hm_txt(d['inicio'])})"), tec)
        if passo in ("ini_m", "fim_m"):
            base = d["inicio"] if passo == "ini_m" else d["fim"]
            h = base // 60
            mins = [0] if h == 24 else [0, 15, 30, 45]
            tec = [[_bt(f"{h:02d}:{m:02d}", f"m:{m}") for m in mins], rodape]
            return (f"{nome}\n\nPasso {self._n(passo)}: minutos", tec)
        if passo == "freq":
            ops = OPCOES_FREQ if item != "ordens" else [1, 5, 10, 15, 30, 60]
            bts = [_bt(_dur_txt(m).replace("a cada ", ""), f"f:{m}") for m in ops]
            tec = [bts[i:i + 3] for i in range(0, len(bts), 3)] + [rodape]
            return (f"{nome}\n\nPasso {self._n('freq')}: com que frequência?", tec)
        if passo == "ordens_fora":
            return (f"{nome}\n\nPasso {self._n('ordens_fora')}: fora do horário, as execuções "
                    "de ordens devem chegar na hora?",
                    [[_bt("⚡ Sim, sempre na hora", "of:1"), _bt("⏳ Não, guardar", "of:0")],
                     rodape])
        if passo == "moedas":
            sel = d.setdefault("moedas", list(self.ag.simbolos_sugestao()))
            opcoes = sorted(set((self.cache.get("simbolos") or []) +
                                [s if s.endswith(self.cfg["moeda_cotacao"]) else s + self.cfg["moeda_cotacao"]
                                 for s in sel + list(self.cfg["simbolos_extras"])]))
            norm = lambda s: s if s.endswith(self.cfg["moeda_cotacao"]) else s + self.cfg["moeda_cotacao"]
            sel_n = [norm(s) for s in sel]
            bts = [_bt(("✅ " if s in sel_n else "▫️ ") + s.replace(self.cfg["moeda_cotacao"], ""),
                       f"moeda:{s}") for s in opcoes[:24]]
            tec = [bts[i:i + 3] for i in range(0, len(bts), 3)]
            tec.append([_bt("Próximo ➡️", "prox")])
            tec.append(rodape)
            return (f"{nome}\n\nQuais moedas a sugestão automática deve analisar?\n"
                    "(outras: /frequencia sugestao 1h BTC ETH SOL)", tec)
        if passo == "confirmar":
            return (f"{nome}\n\nConfira:\n{self._resumo()}",
                    [[_bt("💾 Salvar", "salvar"), _bt("↩️ Recomeçar", "recomecar")], rodape])
        return self.tela_menu()

    def _passos(self):
        item, d = self.wz["item"], self.wz["dados"]
        if item == "moedas":
            return ["moedas", "confirmar"]
        p = [] if item == "geral" else ["ativo"]
        if item != "geral" and d.get("ativo") is False:
            return p + ["confirmar"]
        p.append("dias")
        if d.get("modo") == "custom":
            p += ["ini_h", "ini_m", "fim_h", "fim_m"]
        if item != "geral" and ITENS[item][1]:
            p.append("freq")
        if item in ("geral", "avisos_ordens"):
            p.append("ordens_fora")
        return p + ["confirmar"]

    def _n(self, passo):
        ps = self._passos()
        return f"{ps.index(passo) + 1} de {len(ps)}" if passo in ps else "?"

    def _avancar(self):
        ps = self._passos()
        i = ps.index(self.wz["passo"]) if self.wz["passo"] in ps else -1
        self.wz["passo"] = ps[min(i + 1, len(ps) - 1)]

    def _janela_nova(self):
        d = self.wz["dados"]
        if d.get("modo") == "geral":
            return None
        if d.get("modo") == "sempre":
            return "24/7"
        return {"dias": sorted(d["dias"]), "inicio": d["inicio"], "fim": d["fim"]}

    def _resumo(self):
        item, d = self.wz["item"], self.wz["dados"]
        if item == "moedas":
            return "Moedas: " + ", ".join(d["moedas"])
        if item != "geral" and d.get("ativo") is False:
            return "Desligado"
        j = self._janela_nova()
        if item == "geral":
            linhas = [f"Horário: {_janela_txt(None if j == '24/7' else j)}"]
        else:
            linhas = ["Ligado", "Horário: " + ("horário geral" if j is None else
                                                "24/7" if j == "24/7" else _janela_txt(j))]
        if "freq" in d:
            linhas.append(f"Frequência: {_dur_txt(d['freq'])}")
        if "ordens_fora" in d:
            linhas.append("Execuções fora do horário: " +
                          ("na hora" if d["ordens_fora"] else "guardadas"))
        return "\n".join(linhas)

    def _salvar(self):
        item, d = self.wz["item"], self.wz["dados"]
        if item == "moedas":
            self.ag.st["simbolos_sugestao"] = list(d["moedas"])
            return "✅ Moedas da sugestão salvas"
        if "ordens_fora" in d:
            self.ag.st["ordens_sempre"] = bool(d["ordens_fora"])
        if item == "geral":
            j = self._janela_nova()
            self.ag.st["janela"] = None if j in (None, "24/7") else j
            return "✅ Horário geral salvo"
        it = self.ag.st["itens"][item]
        if d.get("ativo") is False:
            if ITENS[item][1]:
                self.ag.st["freq"][item] = 0
            it["ativo"] = False
            return f"✅ {ITENS[item][0]}: desligado"
        it["ativo"] = True
        it["janela"] = self._janela_nova()
        if ITENS[item][1]:
            self.ag.st["freq"][item] = d.get("freq") or self.ag.freq(item) or 60
            self.ag.st["ultimo"][item] = 0
        return f"✅ {ITENS[item][0]} salvo"

    # ---- clique em botão ------------------------------------------------------
    def on_callback(self, cb_id, msg_id, dado, respondido=False):
        w = self.wz
        self._aviso = ""

        def avisar(txt):
            # o botão já foi confirmado: o aviso aparece no topo do menu
            if respondido:
                self._aviso = txt
            else:
                self.notif.responder_cb(cb_id, txt)

        if not w or w.get("msg") != msg_id:
            if respondido:
                self.notif.enviar("⌛ Este menu expirou. Mande /config de novo.", forcar=True)
            else:
                self.notif.responder_cb(cb_id, "Este menu expirou. Mande /config de novo.")
            return
        if not respondido:
            self.notif.responder_cb(cb_id)
        acao, _, val = dado.partition(":")
        d = w["dados"]
        if acao == "fim":
            w.clear()
            self.notif.editar(msg_id, config_txt(self.cfg, self.ag, self.cache) +
                              "\n\n👍 Configuração concluída. Para mudar de novo: /config")
            return
        if acao == "menu":
            w.update({"item": None, "passo": None, "dados": {}})
            self.notif.editar(msg_id, *self.tela_menu())
            return
        if acao == "item":
            w.update({"item": val, "dados": {}})
            if val == "moedas":
                w["passo"] = "moedas"
            elif val == "geral":
                w["passo"] = "dias"
                d["modo"] = "custom"
            else:
                w["passo"] = "ativo"
        elif acao == "ativo":
            d["ativo"] = val == "1"
            d["modo"] = "custom"
            self._avancar()
        elif acao == "dia":
            sel = d.setdefault("dias", list(range(7)))
            i = int(val)
            sel.remove(i) if i in sel else sel.append(i)
        elif acao == "pre":
            d["dias"] = {"todos": list(range(7)), "uteis": [0, 1, 2, 3, 4], "fds": [5, 6]}[val]
        elif acao == "geral":
            d["modo"] = "geral"
            self._avancar()
        elif acao == "sempre":
            d["modo"] = "sempre"
            self._avancar()
        elif acao == "prox":
            if w["passo"] == "dias" and not d.get("dias"):
                avisar("Marque pelo menos um dia.")
                self.notif.editar(msg_id, *self._tela_aviso())
                return
            if w["passo"] == "moedas" and not d.get("moedas"):
                avisar("Marque pelo menos uma moeda.")
                self.notif.editar(msg_id, *self._tela_aviso())
                return
            if w["passo"] == "dias":
                d["modo"] = "custom"
            self._avancar()
        elif acao == "h":
            chave = "inicio" if w["passo"] == "ini_h" else "fim"
            d[chave] = int(val) * 60
            self._avancar()
            if int(val) == 24:                    # 24:00 não tem minutos
                self._avancar()
        elif acao == "m":
            chave = "inicio" if w["passo"] == "ini_m" else "fim"
            d[chave] = d[chave] // 60 * 60 + int(val)
            if chave == "fim" and d["fim"] == d["inicio"]:
                avisar("O fim não pode ser igual ao início.")
                w["passo"] = "fim_h"
            else:
                self._avancar()
        elif acao == "f":
            d["freq"] = int(val)
            self._avancar()
        elif acao == "of":
            d["ordens_fora"] = val == "1"
            self._avancar()
        elif acao == "moeda":
            q = self.cfg["moeda_cotacao"]
            sel = [s if s.endswith(q) else s + q for s in d.setdefault("moedas", [])]
            sel.remove(val) if val in sel else sel.append(val)
            d["moedas"] = sel
        elif acao == "recomecar":
            w.update({"passo": "moedas" if w["item"] == "moedas" else
                      "dias" if w["item"] == "geral" else "ativo",
                      "dados": {"modo": "custom"} if w["item"] == "geral" else {}})
        elif acao == "salvar":
            aviso = self._salvar()
            w.update({"item": None, "passo": None, "dados": {}})
            self.notif.editar(msg_id, *self.tela_menu(aviso))
            return
        self.notif.editar(msg_id, *self._tela_aviso())

    def _tela_aviso(self):
        texto, teclado = self.tela()
        if getattr(self, "_aviso", ""):
            texto = f"⚠️ {self._aviso}\n\n{texto}"
        return texto, teclado


class Notificador:
    def __init__(self, cfg, state):
        self.token = os.environ.get("TELEGRAM_TOKEN")
        self.chat = os.environ.get("TELEGRAM_CHAT_ID")
        self.cooldown = cfg["cooldown_min"] * 60
        self.enviados = state.setdefault("enviados", {})
        self.state = state
        self.agenda = None
        self.config = None
        self.tg_offset = state.get("tg_offset", 0)
        self.ler_comandos = cfg["comandos_telegram"] and bool(self.token and self.chat)

    # ---- comandos recebidos pelo próprio bot ------------------------------
    def _api(self, metodo, **dados):
        r = requests.post(f"https://api.telegram.org/bot{self.token}/{metodo}",
                          json=dados, timeout=15)
        return r.json()

    def enviar_teclado(self, texto, teclado):
        if not (self.token and self.chat):
            log.info("MENU\n%s", texto)
            return None
        try:
            d = self._api("sendMessage", chat_id=self.chat, text=texto[:4000],
                          reply_markup={"inline_keyboard": teclado})
            return d.get("result", {}).get("message_id")
        except (requests.RequestException, ValueError) as e:
            log.error("Falha ao enviar menu: %s", type(e).__name__)

    def editar(self, msg_id, texto, teclado=None):
        if not (self.token and self.chat and msg_id):
            log.info("MENU\n%s", texto)
            return
        try:
            d = self._api("editMessageText", chat_id=self.chat, message_id=msg_id,
                          text=texto[:4000], reply_markup={"inline_keyboard": teclado or []})
            if not d.get("ok") and "not modified" not in str(d.get("description")):
                log.warning("Telegram recusou a edição: %s", d.get("description"))
        except (requests.RequestException, ValueError) as e:
            log.error("Falha ao editar menu: %s", type(e).__name__)

    def responder_cb(self, cb_id, texto=""):
        if not (self.token and cb_id):
            return
        try:
            d = self._api("answerCallbackQuery", callback_query_id=cb_id, text=texto)
            if not d.get("ok"):
                log.warning("Telegram recusou a resposta ao botão: %s", d.get("description"))
        except (requests.RequestException, ValueError) as e:
            log.warning("Falha ao responder botão: %s", type(e).__name__)

    def registrar_menu(self):
        if not self.ler_comandos:
            return
        try:
            atuais = self._api("getMyCommands").get("result", [])
            nomes = {c["command"] for c in atuais}
            novos = atuais + [c for c in COMANDOS_MENU if c["command"] not in nomes]
            if len(novos) != len(atuais):
                self._api("setMyCommands", commands=novos)
        except (requests.RequestException, ValueError) as e:
            log.warning("Não consegui registrar o menu do bot: %s", type(e).__name__)

    def buscar_comandos(self):
        """Lê mensagens novas do bot (getUpdates). Só aceita o TELEGRAM_CHAT_ID."""
        if not self.ler_comandos:
            return []
        try:
            d = self._api("getUpdates", offset=self.tg_offset,
                          timeout=0, allowed_updates=["message", "callback_query"])
        except (requests.RequestException, ValueError) as e:
            log.warning("Falha ao ler comandos do Telegram: %s", type(e).__name__)
            return []
        if not d.get("ok"):
            if d.get("error_code") == 409:
                self.ler_comandos = False
                log.warning("Outro programa já lê este bot (409). Comandos pelo Telegram "
                            "desligados; use 'pasta_comandos' (veja docs/comandos-externos.md).")
            return []
        textos = []
        for u in d.get("result", []):
            self.tg_offset = u["update_id"] + 1
            cq = u.get("callback_query")
            if cq:
                msg = cq.get("message") or {}
                if str(msg.get("chat", {}).get("id")) == str(self.chat) and \
                        str(cq.get("data", "")).startswith("cfg|"):
                    textos.append({"callback": {"id": cq["id"], "message_id": msg.get("message_id"),
                                                "data": cq["data"]}})
                continue
            m = u.get("message") or {}
            if str(m.get("chat", {}).get("id")) != str(self.chat):
                continue                      # ignora qualquer outro chat
            if time.time() - m.get("date", 0) > 120:
                continue                      # comando antigo (monitor estava parado)
            txt = (m.get("text") or "").strip()
            if txt.startswith("/"):
                textos.append(txt)
        return textos

    # ---- pausa ----------------------------------------------------------
    def pausado(self):
        pz = self.state.get("pausa")
        if pz and pz.get("ate") and time.time() >= pz["ate"]:
            self.retomar(auto=True)
            return False
        return bool(pz)

    def pausar(self, segundos=0):
        agora = time.time()
        pz = self.state.get("pausa") or {"inicio": agora, "suprimidos": 0, "fila": []}
        pz["ate"] = agora + segundos if segundos else 0
        self.state["pausa"] = pz
        ate = (" até " + time.strftime("%d/%m %H:%M", time.localtime(pz["ate"]))) if segundos \
            else " até você mandar /retomar"
        self.enviar(f"⏸️ Alertas pausados{ate}.\n"
                    "Continuo acompanhando o mercado e as ordens; avisos de execução "
                    "ficam guardados e chegam quando retomar.", forcar=True)

    def retomar(self, auto=False):
        pz = self.state.pop("pausa", None)
        if not pz:
            self.enviar("▶️ Os alertas já estão ativos.", forcar=True)
            return
        dur = int((time.time() - pz["inicio"]) / 60)
        txt = (f"▶️ Alertas retomados{' (fim do prazo da pausa)' if auto else ''}. "
               f"Pausa de {dur // 60}h{dur % 60:02d}min; "
               f"{pz['suprimidos']} alerta(s) de mercado não enviados.")
        if pz["fila"]:
            txt += "\n\nAvisos de ordens durante a pausa:\n\n" + "\n\n".join(pz["fila"])
        self.enviar(txt, forcar=True)

    def pode(self, chave):
        agora = time.time()
        # limpa chaves velhas para o estado não crescer sem fim
        for k in [k for k, t in self.enviados.items() if agora - t > 7 * 86400]:
            del self.enviados[k]
        if agora - self.enviados.get(chave, 0) < self.cooldown:
            return False
        self.enviados[chave] = agora
        return True

    def enviar(self, texto, forcar=False):
        if not forcar and self.pausado():
            pz = self.state["pausa"]
            if texto.startswith(CRITICOS):
                pz["fila"] = (pz["fila"] + [time.strftime("%d/%m %H:%M ") + texto])[-20:]
            else:
                pz["suprimidos"] += 1
            log.info("PAUSADO (não enviado)\n%s", texto)
            return
        cat = categoria_msg(texto)
        if not forcar and self.agenda and cat and not self.agenda.ativo(cat):
            log.info("CATEGORIA DESLIGADA (%s, não enviado)\n%s", cat, texto)
            return
        if not forcar and self.agenda and not self.agenda.dentro(cat) and not (
                self.agenda.st["ordens_sempre"] and cat == "avisos_ordens"):
            fora = self.state.setdefault("fora", {"suprimidos": 0, "fila": []})
            if texto.startswith(CRITICOS):
                fora["fila"] = (fora["fila"] + [time.strftime("%d/%m %H:%M ") + texto])[-20:]
            else:
                fora["suprimidos"] += 1
            log.info("FORA DO HORÁRIO (não enviado)\n%s", texto)
            return
        log.info("ALERTA\n%s", texto)
        if not (self.token and self.chat):
            return
        if len(texto) > 4000:                       # limite do Telegram: 4096
            texto = texto[:3990] + "\n[…]"
        try:
            r = requests.post(f"https://api.telegram.org/bot{self.token}/sendMessage",
                              json={"chat_id": self.chat, "text": texto,
                                    "disable_web_page_preview": True}, timeout=15)
            d = r.json()
            if not d.get("ok"):
                log.error("Telegram recusou a mensagem: %s %s",
                          d.get("error_code"), d.get("description"))
        except (requests.RequestException, ValueError) as e:
            # str(e) pode conter a URL com o token: registra só o tipo do erro
            log.error("Falha ao enviar ao Telegram: %s", type(e).__name__)


# --------------------------------------------------------------------------
# Ciclo principal
# --------------------------------------------------------------------------
def carteira(client, cfg, validos, precos):
    quote = cfg["moeda_cotacao"]
    saldos = client.balances()
    posicoes, caixa = {}, 0.0
    for ativo, qtd in saldos.items():
        if ativo.startswith("LD") and ativo[2:] + quote in validos:  # Earn flexível
            ativo = ativo[2:]
        if ativo == quote or ativo in STABLES:
            caixa += qtd
            continue
        sym = ativo + quote
        if sym in validos and sym in precos:
            posicoes[sym] = posicoes.get(sym, 0) + qtd
    posicoes = {s: q for s, q in posicoes.items()
                if q * precos[s] >= cfg["valor_minimo_cotacao"]}
    return posicoes, caixa


def ciclo(client, cfg, state, notif, cache):
    quote = cfg["moeda_cotacao"]
    precos = client.prices()
    agora = time.time()

    if cfg["usar_carteira"] and client.secret and agora - cache.get("cart_t", 0) > 600:
        cache["cart_t"] = agora
        try:
            cache["posicoes"], cache["caixa"] = carteira(client, cfg, cache["validos"], precos)
        except BinanceErro as e:
            # segue monitorando os simbolos_extras mesmo sem acesso à carteira
            codigo = next((c for c in DICAS_ERRO if f"código {c}:" in str(e)), None)
            dica = DICAS_ERRO.get(codigo, "confira a chave no painel da Binance")
            log.error("Falha ao ler a carteira: %s", e)
            if notif.pode("erro_carteira"):
                notif.enviar(f"⚠️ Não consegui ler a carteira na Binance\n{e}\n"
                             f"Provável causa: {dica}\n"
                             "Continuo monitorando só os pares de 'simbolos_extras'.")
    posicoes = cache.get("posicoes", {})
    simbolos = sorted((set(posicoes) | set(cfg["simbolos_extras"]) |
                       cache.get("simbolos_ordens", set())) & cache["validos"])
    cache["simbolos"] = simbolos

    # Variação da carteira como um todo
    if posicoes:
        total = sum(q * precos[s] for s, q in posicoes.items()) + cache.get("caixa", 0)
        ref = state.get("carteira_ref") or total
        state.setdefault("carteira_ref", total)
        var = (total / ref - 1) * 100
        if abs(var) >= cfg["variacao_carteira_pct"]:
            ch = client.change24(list(posicoes))
            movers = sorted(ch.items(), key=lambda kv: abs(kv[1]), reverse=True)[:5]
            notif.enviar("💼 Carteira " + ("subiu" if var > 0 else "caiu") +
                         f" {var:+.1f}% desde o último aviso\n"
                         f"Total: {fmt(total)} {quote} (antes {fmt(ref)})\n"
                         "Maiores variações em 24h: " +
                         ", ".join(f"{s.replace(quote, '')} {p:+.1f}%" for s, p in movers))
            state["carteira_ref"] = total

    resumo_linhas = []
    for sym in simbolos:
        for itv in cfg["intervalos"]:
            try:
                cs = client.klines(sym, itv, cfg["velas"])
            except Exception as e:
                log.warning("%s %s: falha ao buscar velas (%s)", sym, itv, e)
                continue
            if cache.get("drenar"):
                cache["drenar"]()                 # responde comandos sem esperar o ciclo
            st = state.setdefault("pares", {}).setdefault(f"{sym}:{itv}", {})
            resumo, sinais = analyze(cs, precos.get(sym, cs[-1].c), cfg, st)
            if resumo and itv == cfg["intervalos"][0]:
                cache.setdefault("resumos", {})[sym] = resumo
            if resumo:
                resumo_linhas.append(f"{sym} [{itv}] {resumo['tendencia']}, RSI {resumo['rsi']} | "
                                     f"S {resumo['suporte']} | R {resumo['resistencia']}")
            for s in sinais:
                if notif.pode(f"{sym}:{itv}:{s.chave}"):
                    notif.enviar(f"🔔 {sym} [{itv}] — {s.titulo}\n" + "\n".join(s.linhas))

    if resumo_linhas:
        cache["resumo_linhas"], cache["resumo_t"] = resumo_linhas, agora

# --------------------------------------------------------------------------
# Ordens e posições abertas
# --------------------------------------------------------------------------
def alvo_ordem(o):
    """Preço que dispara a ordem e se ele é alcançado com o preço caindo ou subindo."""
    tipo, compra = o["tipo"], o["lado"] == "BUY"
    if "TRAILING" in tipo:
        return None, None
    alvo = o["stop"] if o["stop"] > 0 else o["preco"]
    if alvo <= 0:
        return None, None
    stop = "STOP" in tipo and "TAKE_PROFIT" not in tipo
    # limite/take-profit de compra e stop de venda: disparam com o preço CAINDO
    caindo = (compra and not stop) or (not compra and stop)
    return alvo, caindo


class MonitorOrdens:
    def __init__(self, client, cfg, state, notif, cache):
        self.c, self.cfg, self.notif, self.cache = client, cfg, notif, cache
        self.st = state.setdefault("ordens", {})
        self.st.setdefault("abertas", {})
        self.st.setdefault("posicoes", {})
        self.st.setdefault("alcancadas", [])
        self.st.setdefault("relatorio_t", 0)
        self.futuros = cfg["monitorar_futuros"]

    @staticmethod
    def _norm(o, mercado):
        return {"mercado": mercado, "symbol": o["symbol"], "id": o["orderId"],
                "lado": o["side"], "tipo": o["type"], "preco": float(o.get("price") or 0),
                "stop": float(o.get("stopPrice") or 0), "qtd": float(o["origQty"]),
                "exec": float(o.get("executedQty") or 0)}

    def _ler(self):
        abertas = {f"spot:{o['symbol']}:{o['orderId']}": self._norm(o, "spot")
                   for o in self.c.get("/api/v3/openOrders", signed=True)}
        posicoes = {}
        if self.futuros:
            try:
                for o in self.c.get("/fapi/v1/openOrders", signed=True, base=FAPI_URL):
                    abertas[f"fut:{o['symbol']}:{o['orderId']}"] = self._norm(o, "fut")
                for p in self.c.get("/fapi/v2/positionRisk", signed=True, base=FAPI_URL):
                    amt = float(p["positionAmt"])
                    if amt:
                        posicoes[f"{p['symbol']}:{p.get('positionSide', 'BOTH')}"] = {
                            "symbol": p["symbol"], "qtd": amt,
                            "entrada": float(p["entryPrice"]), "marca": float(p["markPrice"]),
                            "pnl": float(p["unRealizedProfit"]),
                            "liq": float(p.get("liquidationPrice") or 0)}
            except BinanceErro as e:
                self.futuros = False
                log.warning("Futuros desativado: %s", e)
                self.notif.enviar("ℹ️ Não consegui ler Futuros na Binance "
                                  f"({e}). Sigo monitorando só ordens do Spot.")
        return abertas, posicoes

    def _desc(self, o):
        merc = "Futuros" if o["mercado"] == "fut" else "Spot"
        lado = "COMPRA" if o["lado"] == "BUY" else "VENDA"
        alvo, _ = alvo_ordem(o)
        return f"{o['symbol']} {lado} {o['tipo']} ({merc}) {o['qtd']:g}" + \
               (f" @ {fmt(alvo)}" if alvo else "")

    def _status_final(self, o):
        path, base = ("/api/v3/order", BASE_URL) if o["mercado"] == "spot" \
            else ("/fapi/v1/order", FAPI_URL)
        d = self.c.get(path, {"symbol": o["symbol"], "orderId": o["id"]}, signed=True, base=base)
        exe = float(d.get("executedQty") or 0)
        if float(d.get("avgPrice") or 0) > 0:
            medio = float(d["avgPrice"])
        elif exe:
            medio = float(d.get("cummulativeQuoteQty") or 0) / exe
        else:
            medio = 0
        return d.get("status", "?"), exe, medio

    def verificar(self):
        abertas, posicoes = self._ler()
        ant, ant_pos = self.st["abertas"], self.st["posicoes"]
        precos = self.c.prices() if abertas else {}
        self.cache["simbolos_ordens"] = {o["symbol"] for o in abertas.values()}

        # ordens que sumiram: executadas, canceladas ou expiradas
        for k in set(ant) - set(abertas):
            o = ant[k]
            try:
                status, exe, medio = self._status_final(o)
            except Exception as e:
                log.warning("Não consegui consultar a ordem %s: %s", k, type(e).__name__)
                status, exe, medio = "?", 0, 0
            if status == "FILLED" or exe > 0:
                self.notif.enviar(f"✅ ORDEM EXECUTADA\n{self._desc(o)}\n"
                                  f"Executado: {exe:g}" + (f" a {fmt(medio)} (preço médio)" if medio else "") +
                                  ("" if status == "FILLED" else f" — status {status}"))
            else:
                self.notif.enviar(f"❌ Ordem encerrada sem execução ({status})\n{self._desc(o)}")

        for k, o in abertas.items():
            if k not in ant:
                self.notif.enviar(f"🆕 Nova ordem aberta\n{self._desc(o)}")
            elif o["exec"] > ant[k]["exec"] + 1e-12:
                self.notif.enviar(f"🟡 Execução PARCIAL\n{self._desc(o)}\n"
                                  f"Executado: {o['exec']:g} de {o['qtd']:g} "
                                  f"({o['exec'] / o['qtd'] * 100:.0f}%)")
            alvo, caindo = alvo_ordem(o)
            preco = precos.get(o["symbol"])
            if alvo and preco and k not in self.st["alcancadas"] and \
                    ((caindo and preco <= alvo) or (not caindo and preco >= alvo)):
                self.st["alcancadas"].append(k)
                self.notif.enviar(f"🎯 PREÇO DA ORDEM ALCANÇADO\n{self._desc(o)}\n"
                                  f"Preço atual: {fmt(preco)} — aguardando a execução")

        for k in set(ant_pos) - set(posicoes):
            p = ant_pos[k]
            self.notif.enviar(f"🏁 Posição de Futuros encerrada: {p['symbol']} "
                              f"({'LONG' if p['qtd'] > 0 else 'SHORT'} {abs(p['qtd']):g})")
        for k in set(posicoes) - set(ant_pos):
            p = posicoes[k]
            self.notif.enviar(f"🆕 Posição de Futuros aberta: {p['symbol']} "
                              f"{'LONG' if p['qtd'] > 0 else 'SHORT'} {abs(p['qtd']):g} "
                              f"a {fmt(p['entrada'])}")

        self.st["alcancadas"] = [k for k in self.st["alcancadas"] if k in abertas]
        self.st["abertas"], self.st["posicoes"] = abertas, posicoes

        agora = time.time()
        if (abertas or posicoes) and self.notif.agenda and self.notif.agenda.vencido("ordens"):
            self.notif.enviar(self._relatorio(abertas, posicoes, precos))

    def _relatorio(self, abertas, posicoes, precos):
        linhas = ["📋 Posições abertas"]
        for o in sorted(abertas.values(), key=lambda o: o["symbol"]):
            linha = "• " + self._desc(o)
            alvo, _ = alvo_ordem(o)
            preco = precos.get(o["symbol"])
            if alvo and preco:
                linha += f"\n  preço {fmt(preco)} — falta {(alvo / preco - 1) * 100:+.2f}% até a ordem"
            if o["exec"]:
                linha += f"\n  já executado {o['exec'] / o['qtd'] * 100:.0f}%"
            r = self.cache.get("resumos", {}).get(o["symbol"])
            if r:
                linha += f"\n  [{self.cfg['intervalos'][0]}] {r['tendencia']} | S {r['suporte']} | R {r['resistencia']}"
            linhas.append(linha)
        for p in posicoes.values():
            linha = (f"• {p['symbol']} Futuros {'LONG' if p['qtd'] > 0 else 'SHORT'} {abs(p['qtd']):g}"
                     f"\n  entrada {fmt(p['entrada'])} | marca {fmt(p['marca'])} | "
                     f"PnL {p['pnl']:+.2f} USDT")
            if p["liq"] > 0:
                linha += f"\n  liquidação {fmt(p['liq'])} ({(p['liq'] / p['marca'] - 1) * 100:+.1f}%)"
            linhas.append(linha)
        return "\n".join(linhas)


# --------------------------------------------------------------------------
# Comandos vindos do Telegram (o chatbot grava arquivos *.cmd na pasta)
# --------------------------------------------------------------------------
def duracao(txt):
    """'2h', '30m', '1d', '90' (minutos) -> segundos; vazio -> 0 (sem prazo)."""
    txt = (txt or "").strip().lower().replace(" ", "")
    if not txt:
        return 0
    mult = {"m": 60, "min": 60, "h": 3600, "d": 86400}
    for suf in ("min", "m", "h", "d"):
        if txt.endswith(suf) and txt[:-len(suf)].replace(",", ".").replace(".", "", 1).isdigit():
            return int(float(txt[:-len(suf)].replace(",", ".")) * mult[suf])
    return int(float(txt) * 60) if txt.replace(".", "", 1).isdigit() else None


def status_txt(client, cfg, notif, cache, ordens):
    pz = notif.state.get("pausa")
    if pz:
        ate = time.strftime("%d/%m %H:%M", time.localtime(pz["ate"])) if pz["ate"] else "sem prazo"
        cab = f"⏸️ Alertas PAUSADOS ({ate})"
    else:
        cab = "▶️ Alertas ativos"
    linhas = [f"📊 Status do monitor — {cab}"]
    if notif.agenda:
        linhas.append(f"🗓️ Horário: {notif.agenda.janela_txt()}" +
                      ("" if notif.agenda.dentro() else " (agora fora do horário)") +
                      " — detalhes em /agenda")
    if cache.get("resumo_linhas"):
        quando = time.strftime("%H:%M", time.localtime(cache["resumo_t"]))
        linhas += [f"Análise das {quando}:"] + cache["resumo_linhas"]
    else:
        linhas.append("Análise ainda não concluída (aguarde o primeiro ciclo).")
    if ordens:
        ab, pos = ordens.st["abertas"], ordens.st["posicoes"]
        if ab or pos:
            precos = client.prices() if ab else {}
            linhas += ["", ordens._relatorio(ab, pos, precos)]
        else:
            linhas += ["", "📋 Nenhuma ordem ou posição aberta."]
    else:
        linhas += ["", "📋 Monitor de ordens desligado (sem chave ou monitorar_ordens=false)."]
    return "\n".join(linhas)


def coletar_comandos(pasta, notif):
    """Lê comandos do Telegram e da pasta. Não executa nada (pode rodar em thread)."""
    textos = notif.buscar_comandos()
    try:
        arquivos = sorted(pasta.glob("*.cmd"), key=lambda f: f.stat().st_mtime)
    except OSError:
        arquivos = []
    for f in arquivos:
        try:
            conteudo = f.read_text(encoding="utf-8").strip()
            if conteudo.startswith("{"):
                try:
                    conteudo = json.loads(conteudo)
                except ValueError:
                    log.warning("Comando JSON inválido em %s", f.name)
                    conteudo = ""
            textos.append(conteudo)
            f.unlink()
        except OSError as e:
            log.warning("Não consegui ler o comando %s: %s", f.name, e)
    return textos


def processar_comandos(pasta, client, cfg, notif, cache, ordens):
    textos = coletar_comandos(pasta, notif)
    for texto in textos:
        executar_comando(texto, client, cfg, notif, cache, ordens)
    return bool(textos)


class Receptor:
    """Recebe comandos e cliques a cada 0,5 s numa thread própria.

    Cliques em botões são confirmados ao Telegram NA HORA (senão o botão fica
    "carregando" e expira); a execução vai para uma fila que o laço principal
    esvazia com frequência, inclusive no meio da análise.
    """

    def __init__(self, pasta, notif):
        self.pasta, self.notif, self.fila = pasta, notif, queue.Queue()

    def rodar(self):
        while True:
            try:
                for item in coletar_comandos(self.pasta, self.notif):
                    if isinstance(item, dict) and item.get("callback"):
                        self.notif.responder_cb(item["callback"].get("id"))
                        item["callback"]["respondido"] = True
                    self.fila.put(item)
            except Exception:
                log.exception("Erro ao receber comandos")
            time.sleep(0.5)

    def drenar(self, client, cfg, cache, ordens):
        n = 0
        while True:
            try:
                item = self.fila.get_nowait()
            except queue.Empty:
                return n
            executar_comando(item, client, cfg, self.notif, cache, ordens)
            n += 1


AJUDA = {
"alertas": """📖 O QUE SIGNIFICA CADA ALERTA

🔔 Pressão na resistência
O preço está batendo várias vezes num "teto" (resistência). O monitor compara os sinais de que ele vai furar o teto com os sinais de que vai voltar, e dá uma leitura:
• "rompimento iminente": os sinais apontam para furar o teto
• "risco de rejeição": os sinais apontam para voltar para baixo
• "indefinido": empate; vale esperar

🔔 Teste de suporte
O mesmo, mas no "piso" (suporte): o preço está encostando várias vezes nele.
• "perda do suporte iminente": pode furar o piso e cair
• "suporte segurando": os compradores estão defendendo o piso

🔔 Faixa lateral
O preço está "preso" entre o piso e o teto há um tempo, sem tendência clara. A mensagem mostra o piso, o teto, o meio e onde o preço está (0% = no piso, 100% = no teto).

🔔 Rompimento
O preço fechou do outro lado de um piso ou teto. "Confirmado por volume" quer dizer que foi com muitos negócios, o que costuma ser mais confiável. "Volume fraco" pede mais cautela.

🔔 Acomodação
Depois de um rompimento, o preço voltou para "testar" a zona rompida e se manteve do lado novo. É comum o antigo teto virar piso (e vice-versa).

🔔 Falso rompimento
O preço furou a zona mas voltou. O rompimento não se confirmou.

💼 Variação da carteira
O valor total da sua carteira subiu ou caiu mais que o limite configurado.

📊 Resumo
Uma foto de cada moeda: tendência, RSI, piso (S) e teto (R) mais próximos.""",

"termos": """📖 TERMOS USADOS NOS RELATÓRIOS

Suporte (S): região de preço onde ele costuma parar de cair, porque aparecem compradores. Funciona como um "piso".

Resistência (R): região onde ele costuma parar de subir, porque aparecem vendedores. Um "teto".

Zona: o monitor mostra piso e teto como uma faixa de preço (ex.: 80.008–80.764), não como um número exato.

Toques: quantas vezes o preço já virou naquela zona. Mais toques = zona mais forte.

Testes: quantas das últimas velas encostaram na zona.

Vela [1h] / [4h]: cada vela é um período do gráfico. [1h] olha o movimento de hora em hora (curto prazo); [4h] de 4 em 4 horas (prazo um pouco maior). Quando os dois concordam, o sinal é mais forte.

Tendência: alta (subindo de forma consistente), baixa (caindo) ou lateral (sem direção).

RSI: medidor de força de 0 a 100. Acima de 70 = subiu muito rápido ("sobrecomprado"), pode dar uma pausa. Abaixo de 30 = caiu muito rápido ("sobrevendido"), pode repicar. Entre 30 e 70 = normal.

Divergência de RSI: o preço chega no mesmo nível, mas com menos força que antes. Costuma indicar que o movimento está cansando.

Volume: quantidade negociada. "1.9× a média" = quase o dobro do normal.

Pavio: a "linha fina" da vela. Pavio longo em cima = o preço subiu mas foi empurrado de volta (vendedores). Embaixo = compradores defenderam.

Volatilidade comprimida: o preço está se movendo pouco, "acumulando". Muitas vezes vem antes de um movimento forte.

ATR: tamanho médio do movimento de uma vela. O monitor usa para medir o que é "perto" ou "longe" em cada moeda.

Repique: subida curta depois de uma queda.""",

"ordens": """📖 AVISOS DE ORDENS E POSIÇÕES

🆕 Nova ordem/posição: você abriu uma ordem (ou posição em Futuros) e o monitor passou a acompanhar.

🎯 Preço alcançado: o mercado chegou no preço da sua ordem, mas ela ainda não foi executada (pode faltar quem negocie do outro lado).

🟡 Execução parcial: só uma parte da quantidade foi negociada.

✅ Ordem executada: a ordem foi negociada por completo. Mostra o preço médio que você pagou ou recebeu.

❌ Encerrada sem execução: a ordem foi cancelada, expirou ou foi recusada.

🏁 Posição encerrada: uma posição em Futuros foi fechada.

📋 Relatório (a cada 5 min enquanto houver algo aberto):
• "falta X% até a ordem": quanto o preço ainda precisa andar para chegar na sua ordem
• Spot: compra e venda da própria moeda
• Futuros LONG: aposta na alta; SHORT: aposta na queda
• Entrada: preço em que a posição foi aberta
• Marca: preço de referência atual
• PnL: lucro (+) ou prejuízo (−) ainda não realizado
• Liquidação: preço em que a corretora fecha a posição à força. Fique longe dele!

Tipos de ordem:
• LIMIT: compra/vende num preço definido
• STOP_LOSS: vende/compra quando o preço passa de um ponto, para limitar perdas
• TAKE_PROFIT: realiza o lucro quando chega no alvo""",

"tendencia": """📖 COMO LER O /tendencia

Sugestão: ALTA, BAIXA ou LATERAL, com a força (leve, moderada, forte). Vem do "score", de −1 (muito baixista) a +1 (muito altista), que soma vários fatores com pesos fixos.

Alvos antes da ruptura: o teto (resistência) e o piso (suporte) mais próximos. É até onde o preço tende a ir antes de romper para um lado.

Chance de tocar primeiro: pela distância até o teto e até o piso, ajustada pelo score. Ex.: teto 65% quer dizer que, com os dados de agora, é mais provável encostar no teto antes do piso.

Tempo típico: estimativa de quantas horas o preço leva para tocar um dos lados, pela volatilidade recente.

Faixa provável em 24h: onde o preço deve estar daqui a um dia em ~68% dos casos, pela volatilidade.

Fatores:
• Tendência: direção das médias em cada tempo gráfico
• EMAs 9/21/55/200: médias alinhadas para cima = tendência saudável; preço muito longe da EMA 200 = esticado
• Bollinger: bandas apertadas avisam que um movimento forte pode vir; preço fora da banda = esticado
• Regime diário: o termômetro do /regime
• RSI: força; extremos contam contra (risco de correção ou repique)
• Fluxo agressor: % do volume em compras "a mercado" (quem tem pressa de comprar)
• Livro de ofertas: quanto há de ordens de compra x venda perto do preço
• Open interest: dinheiro em contratos futuros. Subindo junto com o preço = movimento com força
• Funding: taxa entre comprados e vendidos no futuro. Muito alta = muitos apostando na alta (sinal contrário)
• Long/short: proporção de contas compradas x vendidas (extremos = sinal contrário)

⚠️ É uma estimativa, não previsão. Os pesos não foram testados no histórico.""",
"regime": """📖 COMO LER O /regime

É o "termômetro" do mercado, calculado uma vez a cada poucas horas com dados diários. Diz se o ambiente está a favor ou contra, independente do gráfico de curto prazo.

Regimes: EUFORIA (preço muito acima do custo médio, ganância), OTIMISMO, NEUTRO, MEDO, CAPITULAÇÃO (pânico ou preço abaixo do custo médio dos investidores).

Viés macro: de −1 (ambiente desfavorável) a +1 (favorável).

Itens:
• MVRV Z-Score: quanto o preço está acima do preço médio pago por todos os bitcoins. Muito alto (>6) costuma marcar topos; abaixo de 0, fundos
• Fluxo de corretoras: BTC entrando nas corretoras costuma ser para vender; saindo, para guardar (acumulação)
• Saldo nas corretoras: menos BTC disponível para venda tende a ajudar o preço
• Endereços ativos: rede sendo mais ou menos usada
• Fear & Greed: medo/ganância do varejo. Leitura contrária: medo extremo costuma ser oportunidade; ganância extrema, risco
• Juros do Fed: cortes aumentam a liquidez (bom para ativos de risco)
• Dólar: o bitcoin costuma andar ao contrário do dólar
• Notícias: manchetes das últimas 24h classificadas por um modelo de IA local como alta, baixa ou neutra

🚨 Extremos convergentes: no /tendencia, quando euforia + funding alto + RSI sobrecomprado aparecem juntos (ou o inverso), o risco de reversão brusca é maior.

O regime entra no /tendencia como um dos fatores. Você recebe um aviso quando o regime muda.""",
"agenda": """📖 AGENDA E FREQUÊNCIA

O jeito mais fácil é o /config: ele mostra tudo o que está sendo verificado e, se você quiser alterar, guia com botões: escolha o item (ex.: Regime) → ligado/desligado → dias da semana → hora de início → hora de fim → frequência → salvar. Cada categoria pode ter o próprio horário ou seguir o horário geral.

Por texto, /agenda mostra o horário e a frequência de cada relatório.

Horário de atividade (fora dele, nada é enviado; os avisos ficam guardados e chegam quando o horário abrir):
• /agenda 24/7: sempre ativo
• /agenda uteis 08:00-18:00: dias úteis, horário comercial
• /agenda todos 07:00-23:00: todos os dias
• /agenda seg-sex 09:00-17:30 ou /agenda seg,qua,sex 09:00-17:00
• /agenda 22:00-06:00: atravessa a meia-noite

Execução de ordens fora do horário:
• /agenda ordens sempre: ✅ execuções e cancelamentos chegam na hora, mesmo fora do horário (padrão)
• /agenda ordens guardar: também esperam o horário abrir

Frequência dos relatórios (só dentro do horário):
• /frequencia analise 30m: 📊 tendência, RSI, suporte e resistência de cada par
• /frequencia sugestao 1h: 📈 o /tendencia automático. Para escolher as moedas: /frequencia sugestao 1h BTC ETH
• /frequencia regime 2h: 🌡️ o /regime automático
• /frequencia ordens 5m: 📋 relatório de ordens abertas
• Use off para desligar. Ex.: /frequencia sugestao off

Alertas de padrão (🔔) e mudança de regime não têm frequência: chegam quando acontecem.""",
"comandos": """📖 COMANDOS

/status: análise atual de cada moeda e suas ordens/posições abertas
/pausar: para de enviar alertas até você mandar /retomar
/pausar 30m, /pausar 2h, /pausar 1d: pausa por um tempo e volta sozinho
/retomar: volta a enviar e entrega os avisos de ordens guardados na pausa
/tendencia: sugestão estatística de alta ou baixa e dos alvos (piso e teto) antes da ruptura. Use /tendencia ETH para outra moeda
/regime: termômetro diário do mercado (on-chain, sentimento, notícias, juros e dólar)
/config: mostra o que está sendo verificado e permite alterar tudo com botões, passo a passo
/agenda: horário de atividade (ex.: /agenda uteis 08:00-18:00 ou /agenda 24/7)
/frequencia: frequência de cada relatório (ex.: /frequencia regime 2h)
/ajuda: esta explicação completa
/ajuda alertas, termos, ordens, tendencia, regime, agenda ou comandos: só uma parte

Durante a pausa o monitor continua acompanhando tudo. Avisos de execução de ordens nunca se perdem: ficam guardados e chegam quando você retomar.

⚠️ O monitor identifica padrões, não prevê o futuro e não é recomendação de compra ou venda.""",
}
ORDEM_AJUDA = ["alertas", "termos", "ordens", "tendencia", "regime", "agenda", "comandos"]
SINONIMOS_AJUDA = {"alerta": "alertas", "termo": "termos", "glossario": "termos",
                   "glossário": "termos", "ordem": "ordens", "posicoes": "ordens",
                   "posições": "ordens", "comando": "comandos", "tendência": "tendencia",
                   "frequencia": "agenda", "frequência": "agenda", "horario": "agenda",
                   "horário": "agenda"}


# --------------------------------------------------------------------------
# Camada de REGIME (dados diários): on-chain, sentimento, notícias e macro.
# Roda numa thread própria para nunca atrasar o acompanhamento de ordens.
# --------------------------------------------------------------------------
CM_URL = "https://community-api.coinmetrics.io/v4/timeseries/asset-metrics"
CM_METRICAS = ["CapMrktCurUSD", "CapMVRVCur", "FlowInExNtv", "FlowOutExNtv",
               "SplyExNtv", "AdrActCnt"]
FRED_CSV = "https://fred.stlouisfed.org/graph/fredgraph.csv?id="
UA_DADOS = {"User-Agent": "monitor-cripto/1.0"}


def _http(url, timeout=30, **kw):
    r = requests.get(url, headers=UA_DADOS, timeout=timeout, **kw)
    r.raise_for_status()
    return r


def baixar_coinmetrics():
    """Histórico diário do BTC (Coin Metrics Community, CC BY-NC 4.0)."""
    linhas, url = [], CM_URL
    params = {"assets": "btc", "metrics": ",".join(CM_METRICAS), "frequency": "1d",
              "page_size": 10000, "start_time": "2010-07-18"}
    for _ in range(5):
        d = _http(url, params=params, timeout=60).json()
        linhas += d.get("data", [])
        url, params = d.get("next_page_url"), None
        if not url:
            break
    linhas.sort(key=lambda x: x["time"])
    return linhas


def serie(linhas, campo):
    return [float(x[campo]) for x in linhas if x.get(campo) not in (None, "")]


def baixar_fred(id_serie):
    out = []
    for linha in _http(FRED_CSV + id_serie).text.strip().splitlines()[1:]:
        data, _, valor = linha.partition(",")
        if valor not in (".", ""):
            out.append((data, float(valor)))
    return out


def desvio(xs):
    m = mean(xs)
    return (sum((x - m) ** 2 for x in xs) / len(xs)) ** 0.5 if xs else 0


def classificar_manchetes(cfg, titulos):
    """Classifica manchetes com o modelo local do Ollama: +1 alta, -1 baixa, 0 neutro."""
    prompt = ("You are a crypto market analyst. For each headline, say if its likely "
              "short-term impact on the Bitcoin price is ALTA (up), BAIXA (down) or "
              "NEUTRO (neutral/unrelated). Return JSON {\"r\": [..]} with one word per "
              "headline, in the same order.\n\n" +
              "\n".join(f"{i + 1}. {t}" for i, t in enumerate(titulos)))
    r = requests.post(cfg["ollama_url"].rstrip("/") + "/api/generate", timeout=180, json={
        "model": cfg["ollama_modelo"], "prompt": prompt, "stream": False,
        "format": "json", "options": {"temperature": 0}})
    r.raise_for_status()
    resp = json.loads(r.json()["response"]).get("r", [])
    if len(resp) != len(titulos):
        raise ValueError(f"modelo devolveu {len(resp)} classes para {len(titulos)} manchetes")
    mapa = {"ALTA": 1, "BAIXA": -1, "NEUTRO": 0}
    return [mapa.get(str(x).strip().upper(), 0) for x in resp]


def ler_feeds(cfg):
    import xml.etree.ElementTree as ET
    from email.utils import parsedate_to_datetime
    itens = []
    for url in cfg["feeds_rss"]:
        try:
            raiz = ET.fromstring(_http(url, timeout=20).content)
        except Exception as e:
            log.info("Feed indisponível %s: %s", url, type(e).__name__)
            continue
        fonte = url.split("/")[2].replace("www.", "")
        for it in raiz.iter("item"):
            titulo = (it.findtext("title") or "").strip()
            try:
                t = parsedate_to_datetime(it.findtext("pubDate")).timestamp()
            except Exception:
                t = time.time()
            if titulo:
                itens.append({"titulo": titulo, "fonte": fonte, "t": t})
    return itens


class Regime:
    """Calcula e guarda o regime em regime.json (separado do estado principal)."""

    def __init__(self, cfg, cache):
        self.cfg, self.cache = cfg, cache
        self.arq = APP_DIR / "regime.json"
        try:
            self.dados = json.loads(self.arq.read_text())
        except (OSError, ValueError):
            self.dados = {}
        self.dados.setdefault("noticias", [])
        cache["regime"] = self.dados.get("resultado")

    def salvar(self):
        tmp = self.arq.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.dados, ensure_ascii=False))
        tmp.replace(self.arq)

    # ---- notícias -------------------------------------------------------
    def atualizar_noticias(self):
        agora = time.time()
        conhecidas = {n["titulo"] for n in self.dados["noticias"]}
        novas = [i for i in ler_feeds(self.cfg)
                 if i["titulo"] not in conhecidas and agora - i["t"] < 48 * 3600]
        novas = list({n["titulo"]: n for n in novas}.values())[:40]
        for k in range(0, len(novas), 10):
            lote = novas[k:k + 10]
            try:
                classes = classificar_manchetes(self.cfg, [n["titulo"] for n in lote])
            except Exception as e:
                log.warning("Classificação de notícias indisponível: %s", str(e)[:120])
                return
            for n, c in zip(lote, classes):
                n["classe"] = c
            self.dados["noticias"] += lote
        self.dados["noticias"] = [n for n in self.dados["noticias"]
                                  if agora - n["t"] < 48 * 3600 and "classe" in n]
        self.salvar()

    # ---- regime ---------------------------------------------------------
    def calcular(self):
        comp, avisos = [], []

        def add(nome, valor, peso, texto):
            # decimais com vírgula; \x00 marca o separador de milhar (ponto)
            texto = re.sub(r"(?<=\d)\.(?=\d)", ",", texto).replace("\x00", ".")
            comp.append({"nome": nome, "v": _clip(valor), "peso": peso, "texto": texto})

        # On-chain (Coin Metrics)
        try:
            cm = baixar_coinmetrics()
            ult = cm[-1]
            mc = serie(cm, "CapMrktCurUSD")
            pares = [(float(x["CapMrktCurUSD"]), float(x["CapMVRVCur"])) for x in cm
                     if x.get("CapMrktCurUSD") and x.get("CapMVRVCur")]
            mc_u, mvrv_u = pares[-1]
            rc = mc_u / mvrv_u                     # realized cap = market cap / MVRV
            z = (mc_u - rc) / (desvio([p[0] for p in pares]) or 1)
            add("mvrv", (2 - z) / 3, 0.20,
                f"MVRV Z-Score {z:.2f} (MVRV {mvrv_u:.2f}): " +
                ("zona de euforia/topo histórico" if z > 6 else "caro" if z > 3 else
                 "barato, abaixo do custo médio" if z < 0 else "neutro"))
            liq = [float(x["FlowInExNtv"]) - float(x["FlowOutExNtv"]) for x in cm
                   if x.get("FlowInExNtv") and x.get("FlowOutExNtv")]
            if len(liq) > 400:
                s7 = [sum(liq[i - 7:i]) for i in range(len(liq) - 365, len(liq) + 1)]
                zf = (s7[-1] - mean(s7)) / (desvio(s7) or 1)
                qtd = f"{s7[-1]:+,.0f}".replace(",", "\x00")
                add("fluxo", -zf / 2, 0.15,
                    f"Fluxo de corretoras 7d: {qtd} BTC "
                    f"({'entrada: pressão de venda' if s7[-1] > 0 else 'saída: acumulação'}; "
                    f"z {zf:+.1f})")
            sx = serie(cm, "SplyExNtv")
            if len(sx) > 31:
                ch = sx[-1] / sx[-31] - 1
                add("saldo", -ch / 0.02, 0.10,
                    f"Saldo de BTC nas corretoras 30d: {ch * 100:+.1f}% "
                    f"({'caindo: menos oferta à venda' if ch < 0 else 'subindo: mais oferta'})")
            ad = serie(cm, "AdrActCnt")
            if len(ad) > 365:
                ratio = mean(ad[-30:]) / mean(ad[-365:])
                add("enderecos", (ratio - 1) / 0.15, 0.10,
                    f"Endereços ativos (média 30d vs 1 ano): {(ratio - 1) * 100:+.1f}%")
            onchain_data = ult["time"][:10]
        except Exception as e:
            avisos.append(f"on-chain indisponível ({type(e).__name__})")
            z, onchain_data = None, None

        # Fear & Greed (Alternative.me)
        fg = None
        try:
            d = _http("https://api.alternative.me/fng/?limit=1").json()["data"][0]
            fg = int(d["value"])
            add("fg", (50 - fg) / 35, 0.15,
                f"Fear & Greed {fg} ({d['value_classification']}): leitura contrária")
        except Exception as e:
            avisos.append(f"Fear & Greed indisponível ({type(e).__name__})")

        # Macro (FRED)
        try:
            teto = baixar_fred("DFEDTARU")
            ant = [v for dt, v in teto if dt <= time.strftime(
                "%Y-%m-%d", time.gmtime(time.time() - 90 * 86400))]
            mud = teto[-1][1] - (ant[-1] if ant else teto[-1][1])
            add("fed", -mud / 0.5, 0.05,
                f"Juros do Fed: teto {teto[-1][1]:.2f}%, {mud:+.2f} p.p. em 90d" +
                (" (cortes: mais liquidez)" if mud < 0 else " (altas: menos liquidez)" if mud > 0 else ""))
        except Exception as e:
            avisos.append(f"juros do Fed indisponível ({type(e).__name__})")
        try:
            usd = baixar_fred("DTWEXBGS")
            ch = usd[-1][1] / usd[-22][1] - 1
            add("dolar", -ch / 0.02, 0.10,
                f"Dólar amplo (≈DXY) ~30d: {ch * 100:+.1f}% "
                f"({'dólar forte pesa contra' if ch > 0 else 'dólar fraco ajuda'})")
        except Exception as e:
            avisos.append(f"dólar indisponível ({type(e).__name__})")

        # Notícias (classificadas pelo modelo local)
        ns = [n for n in self.dados["noticias"] if time.time() - n["t"] < 24 * 3600]
        if len(ns) >= 5:
            alta = sum(n["classe"] > 0 for n in ns) / len(ns)
            baixa = sum(n["classe"] < 0 for n in ns) / len(ns)
            add("noticias", (alta - baixa) * 2, 0.15,
                f"Notícias 24h: {len(ns)} manchetes, {alta * 100:.0f}% alta / "
                f"{baixa * 100:.0f}% baixa")
        else:
            avisos.append("notícias: poucas manchetes classificadas nas últimas 24h")

        peso = sum(c["peso"] for c in comp)
        score = sum(c["v"] * c["peso"] for c in comp) / peso if peso else 0
        if (z is not None and z > 6) or (fg is not None and fg >= 80 and (z or 0) > 3):
            nome = "EUFORIA"
        elif (z is not None and z < 0) or (fg is not None and fg <= 15):
            nome = "CAPITULAÇÃO"
        elif fg is not None and fg < 40:
            nome = "MEDO"
        elif fg is not None and fg > 60:
            nome = "OTIMISMO"
        else:
            nome = "NEUTRO"
        res = {"nome": nome, "score": score, "componentes": comp, "avisos": avisos,
               "z": z, "fg": fg, "onchain_data": onchain_data, "t": time.time()}
        anterior = (self.dados.get("resultado") or {}).get("nome")
        if anterior and anterior != nome:
            self.cache["regime_mudou"] = (anterior, nome)
        self.dados["resultado"] = res
        self.cache["regime"] = res
        self.salvar()
        return res

    def rodar(self):
        """Laço da thread: notícias a cada N min, regime a cada N horas."""
        ult_not = ult_reg = 0
        while True:
            agora = time.time()
            try:
                if agora - ult_not >= self.cfg["noticias_min"] * 60:
                    ult_not = agora
                    self.atualizar_noticias()
                if agora - ult_reg >= self.cfg["regime_horas"] * 3600 or \
                        self.cache.pop("regime_forcar", False):
                    ult_reg = agora
                    self.calcular()
                    if self.cache.pop("regime_enviar", False):
                        self.cache["regime_pronto"] = True
            except Exception:
                log.exception("Erro na camada de regime")
            time.sleep(10)


def regime_txt(r):
    if not r:
        return "🌡️ Regime ainda não calculado (o primeiro cálculo leva 1–2 minutos após iniciar)."
    forca = "neutro" if abs(r["score"]) < 0.15 else ("favorável" if r["score"] > 0 else "desfavorável")
    linhas = [f"🌡️ Regime de mercado: {r['nome']} | viés macro {r['score']:+.2f} ({forca})"
              .replace(".", ",", 1)]
    for c in r["componentes"]:
        s = "+" if c["v"] > 0.1 else "−" if c["v"] < -0.1 else "="
        linhas.append(f"{s} {c['texto']}")
    if r["avisos"]:
        linhas.append("ℹ️ " + "; ".join(r["avisos"]))
    linhas += ["", "Atualizado: " + time.strftime("%d/%m %H:%M", time.localtime(r["t"])) +
               (f" | on-chain de {r['onchain_data']}" if r.get("onchain_data") else ""),
               "Fontes: Coin Metrics Community (CC BY-NC 4.0), Fear & Greed Index por "
               "Alternative.me, FRED/St. Louis Fed, feeds RSS de notícias "
               "(classificação por modelo local)."]
    return "\n".join(linhas)


def extremos_convergentes(regime, funding_pct, rsi_v):
    """Euforia + funding alto + RSI sobrecomprado (ou o inverso): risco de reversão."""
    if not regime or funding_pct is None:
        return None
    fg = regime.get("fg") or 50
    if (regime["nome"] == "EUFORIA" or fg >= 75) and funding_pct > 0.03 and rsi_v > 70:
        return ("🚨 Extremos convergentes de ALTA (euforia + funding alto + RSI sobrecomprado): "
                "risco elevado de reversão para baixo / liquidação de comprados.")
    if (regime["nome"] == "CAPITULAÇÃO" or fg <= 25) and funding_pct < 0 and rsi_v < 30:
        return ("🚨 Extremos convergentes de BAIXA (medo extremo + funding negativo + RSI "
                "sobrevendido): risco elevado de reversão para cima / short squeeze.")
    return None


# --------------------------------------------------------------------------
# /tendencia: sugestão estatística de direção e alvos dentro da faixa
# --------------------------------------------------------------------------
def _clip(x, a=-1.0, b=1.0):
    return max(a, min(b, x))


def _publico(client, path, params, base=BASE_URL):
    """Dado público opcional: falha vira None, sem derrubar o comando."""
    try:
        return client.get(path, params, base=base)
    except Exception as e:
        log.info("Dado indisponível %s: %s", path, type(e).__name__)
        return None


def zonas_de(cs, cfg):
    A = wilder(true_ranges(cs), 14)[-1]
    topos, fundos = pivots(cs, cfg["pivo_janela"])
    return build_zones(topos, fundos, A, cfg), A


def analisar_tendencia(client, cfg, simbolo, cache=None):
    """Retorna o texto da resposta do /tendencia para um par."""
    quote = cfg["moeda_cotacao"]
    sym = simbolo.upper()
    if not sym.endswith(quote):
        sym += quote
    fatores, score, peso_total = [], 0.0, 0.0

    def soma(valor, peso, texto):
        nonlocal score, peso_total
        score += _clip(valor) * peso
        peso_total += peso
        sinal = "+" if valor > 0.1 else "−" if valor < -0.1 else "="
        fatores.append(f"{sinal} " + re.sub(r"(?<=\d)\.(?=\d)", ",", texto))

    # 1) Velas e análise técnica de cada intervalo
    velas, resumo = {}, {}
    for itv in cfg["intervalos"]:
        try:
            cs = client.klines(sym, itv, cfg["velas"])
        except BinanceErro:
            return f"Par {sym} não encontrado na Binance. Exemplo: /tendencia BTC"
        if len(cs) < 80:
            continue
        velas[itv] = cs
        r, _ = analyze(cs, cs[-1].c, cfg, {})
        resumo[itv] = r
    if not velas:
        return f"Sem dados suficientes para {sym}."
    base_itv = cfg["intervalos"][0]
    cs = velas[base_itv]
    preco = float(client.get("/api/v3/ticker/price", {"symbol": sym})["price"])

    peso_itv = 0.30 / len(resumo)
    for itv, r in resumo.items():
        v = {"alta": 1, "baixa": -1}.get(r["tendencia"], 0)
        soma(v, peso_itv, f"Tendência [{itv}]: {r['tendencia']}")

    rsi_v = resumo[base_itv]["rsi"]
    if rsi_v > 70:
        soma(-0.5, 0.10, f"RSI [{base_itv}] {rsi_v}: sobrecomprado, risco de correção")
    elif rsi_v < 30:
        soma(0.5, 0.10, f"RSI [{base_itv}] {rsi_v}: sobrevendido, chance de repique")
    else:
        soma((rsi_v - 50) / 20, 0.10, f"RSI [{base_itv}] {rsi_v}")

    # EMAs 9/21/55/200: alinhamento das médias (tendência micro → macro)
    C = [c.c for c in cs]
    emas = {n: ema(C, n)[-1] for n in (9, 21, 55, 200)}
    ordem = [emas[9], emas[21], emas[55], emas[200]]
    alinh = sum(1 if ordem[i] > ordem[i + 1] else -1 for i in range(3)) / 3
    dist200 = (preco / emas[200] - 1) * 100
    soma(alinh, 0.10, f"EMAs 9/21/55/200 [{base_itv}]: " +
         ("alinhadas para cima" if alinh == 1 else "alinhadas para baixo" if alinh == -1
          else "misturadas") + f"; preço {dist200:+.1f}% da EMA 200".replace(".", ","))

    # Bandas de Bollinger 20/2: posição e compressão (squeeze)
    jan = C[-20:]
    mb, dp = mean(jan), desvio(jan)
    if dp > 0:
        pct_b = (preco - (mb - 2 * dp)) / (4 * dp)
        larg = [desvio(C[i - 20:i]) / mean(C[i - 20:i]) for i in range(len(C) - 120, len(C) + 1)]
        aperto = sorted(larg).index(larg[-1]) / len(larg)
        txt = f"Bollinger [{base_itv}]: %B {pct_b * 100:.0f}%"
        if aperto < 0.15:
            txt += ", bandas apertadas (squeeze): movimento forte costuma vir a seguir"
        v = -0.4 if pct_b > 1 else 0.4 if pct_b < 0 else 0
        if v:
            txt += ", fora da banda (esticado: tendência a voltar)"
        soma(v, 0.05, txt)

    # 2) Fluxo: compras agressivas x vendas agressivas nas últimas 24 velas
    vol = sum(c.v for c in cs[-24:])
    if vol > 0 and any(c.tb for c in cs[-24:]):
        compra = sum(c.tb for c in cs[-24:]) / vol
        soma((compra - 0.5) * 10, 0.12,
             f"Fluxo agressor [{base_itv}, 24 velas]: {compra * 100:.0f}% compras")

    # 3) Livro de ofertas: volume de compra x venda até ±1% do preço
    livro = _publico(client, "/api/v3/depth", {"symbol": sym, "limit": 500})
    if livro:
        bid = sum(float(q) for p, q in livro["bids"] if float(p) >= preco * 0.99)
        ask = sum(float(q) for p, q in livro["asks"] if float(p) <= preco * 1.01)
        if bid + ask > 0:
            imb = (bid - ask) / (bid + ask)
            soma(imb * 2, 0.10, f"Livro de ofertas (±1%): {bid / (bid + ask) * 100:.0f}% do lado da compra")

    # 4) Mercado futuro (público): open interest, funding, long/short
    oi = _publico(client, "/futures/data/openInterestHist",
                  {"symbol": sym, "period": "1h", "limit": 5}, base=FAPI_URL)
    if oi and len(oi) >= 2:
        o0, o1 = float(oi[0]["sumOpenInterest"]), float(oi[-1]["sumOpenInterest"])
        d_oi = (o1 / o0 - 1) if o0 else 0
        d_px = cs[-1].c / cs[-5].c - 1
        lado = 1 if d_px > 0 else -1
        if d_oi > 0.005:
            soma(lado * 0.8, 0.08, f"Open interest {d_oi * 100:+.1f}% em 4h com preço "
                 f"{'subindo' if lado > 0 else 'caindo'}: dinheiro novo a favor do movimento")
        elif d_oi < -0.005:
            soma(lado * 0.3, 0.08, f"Open interest {d_oi * 100:+.1f}% em 4h: posições fechando, "
                 "movimento perde combustível")
        else:
            soma(0, 0.08, "Open interest estável em 4h")
    prem = _publico(client, "/fapi/v1/premiumIndex", {"symbol": sym}, base=FAPI_URL)
    funding_pct = None
    if prem and "lastFundingRate" in prem:
        f = funding_pct = float(prem["lastFundingRate"]) * 100
        if f > 0.03:
            soma(-0.6, 0.04, f"Funding {f:.3f}%: muitos comprados alavancados (risco de queda)")
        elif f < -0.01:
            soma(0.6, 0.04, f"Funding {f:.3f}%: muitos vendidos alavancados (risco de alta)")
        else:
            soma(0, 0.04, f"Funding {f:.3f}%: neutro")
    ls = _publico(client, "/futures/data/globalLongShortAccountRatio",
                  {"symbol": sym, "period": "1h", "limit": 1}, base=FAPI_URL)
    if ls:
        r_ls = float(ls[-1]["longShortRatio"])
        v = -0.5 if r_ls > 2 else 0.5 if r_ls < 0.8 else 0
        soma(v, 0.04, f"Contas long/short: {r_ls:.2f}" +
             (" (multidão muito comprada: sinal contrário)" if r_ls > 2 else
              " (multidão muito vendida: sinal contrário)" if r_ls < 0.8 else ""))

    # Camada de regime (diária)
    regime = (cache or {}).get("regime")
    if regime and time.time() - regime["t"] < 2 * 86400:
        soma(regime["score"] * 1.5, cfg["regime_peso_tendencia"],
             f"Regime diário: {regime['nome']} (viés macro {regime['score']:+.2f}; /regime)"
             .replace(".", ",", 1))
    alerta_ext = extremos_convergentes(regime, funding_pct, rsi_v)

    score = score / peso_total if peso_total else 0

    # 5) Alvos: piso e teto mais próximos no intervalo base
    zonas, A = zonas_de(cs, cfg)
    res = min((z for z in zonas if z.low > preco), key=lambda z: z.low, default=None)
    sup = max((z for z in zonas if z.high < preco), key=lambda z: z.high, default=None)
    rets = [math.log(cs[i].c / cs[i - 1].c) for i in range(len(cs) - 200, len(cs))]
    sig = (sum(r * r for r in rets) / len(rets) - mean(rets) ** 2) ** 0.5 or 1e-9
    passos_24h = {"1m": 1440, "3m": 480, "5m": 288, "15m": 96, "30m": 48, "1h": 24,
                  "2h": 12, "4h": 6, "6h": 4, "8h": 3, "12h": 2, "1d": 1}.get(base_itv, 24)
    s24 = sig * math.sqrt(passos_24h)
    alvo_up = res.low if res else preco * math.exp(s24)
    alvo_dn = sup.high if sup else preco * math.exp(-s24)
    a, b = math.log(alvo_up / preco), math.log(preco / alvo_dn)
    p_geo = b / (a + b) if a + b > 0 else 0.5
    p_geo = min(max(p_geo, 0.02), 0.98)
    p_up = 1 / (1 + math.exp(-(math.log(p_geo / (1 - p_geo)) + 1.5 * score)))
    horas = a * b / (sig * sig) / passos_24h * 24 if sig else 0

    forca = abs(score)
    if forca < 0.15:
        dir_txt, emoji = "LATERAL / indefinida", "↔️"
    else:
        nivel = "leve" if forca < 0.35 else "moderada" if forca < 0.6 else "forte"
        dir_txt, emoji = (f"ALTA {nivel}", "📈") if score > 0 else (f"BAIXA {nivel}", "📉")

    def zona_alvo(z, lado):
        if not z:
            return f"sem {'teto' if lado > 0 else 'piso'} mapeado (alvo pela volatilidade de 24h)"
        return zona_txt(z)

    def pct(x):
        return f"{(x / preco - 1) * 100:+.2f}%".replace(".", ",")

    if score >= 0.15 and p_up >= 0.5:
        leitura = f"viés de alta; valor provável antes da ruptura: o teto em {fmt(alvo_up)}."
    elif score <= -0.15 and p_up < 0.5:
        leitura = f"viés de baixa; valor provável antes da ruptura: o piso em {fmt(alvo_dn)}."
    elif score <= -0.15:
        leitura = (f"viés de baixa, mas o teto está mais perto: pode encostar em {fmt(alvo_up)} "
                   f"e ser rejeitado antes de buscar o piso em {fmt(alvo_dn)}.")
    elif score >= 0.15:
        leitura = (f"viés de alta, mas o piso está mais perto: pode testar {fmt(alvo_dn)} "
                   f"antes de buscar o teto em {fmt(alvo_up)}.")
    else:
        leitura = f"sem viés claro; tende a oscilar entre {fmt(alvo_dn)} e {fmt(alvo_up)}."

    acima = min((z for z in zonas if res and z.low > res.high), key=lambda z: z.low, default=None)
    abaixo = max((z for z in zonas if sup and z.high < sup.low), key=lambda z: z.high, default=None)
    linhas = [
        f"{emoji} Tendência {sym} — sugestão: {dir_txt} (score {score:+.2f})".replace(".", ","),
        f"Preço: {fmt(preco)}",
        "",
        "Alvos antes da ruptura (intervalo " + base_itv + "):",
        f"⬆️ Teto: {fmt(alvo_up)} ({pct(alvo_up)}) — {zona_alvo(res, 1)}",
        f"⬇️ Piso: {fmt(alvo_dn)} ({pct(alvo_dn)}) — {zona_alvo(sup, -1)}",
        f"Chance estimada de tocar primeiro: teto {p_up * 100:.0f}% | piso {(1 - p_up) * 100:.0f}%",
        f"Leitura: {leitura}",
    ]
    if horas:
        linhas.append(f"Tempo típico até tocar um dos lados: ~{horas:.0f}h" if horas >= 1
                      else "Tempo típico até tocar um dos lados: menos de 1h")
    linhas += [
        f"Se romper o teto, próximo alvo: {zona_txt(acima) if acima else 'sem zona mapeada'}",
        f"Se romper o piso, próximo alvo: {zona_txt(abaixo) if abaixo else 'sem zona mapeada'}",
        f"Faixa provável em 24h (68%): {fmt(preco * math.exp(-s24))} – {fmt(preco * math.exp(s24))}",
        "",
        "Fatores considerados:",
    ] + fatores + ([ "", alerta_ext] if alerta_ext else []) + [
        "",
        "⚠️ Estimativa estatística com os dados deste momento, não previsão nem "
        "recomendação. Os pesos dos fatores são fixos e não foram testados no "
        "histórico. Use como mais um elemento de análise. /ajuda tendencia explica cada item.",
    ]
    return "\n".join(linhas)


def executar_comando(texto, client, cfg, notif, cache, ordens):
    if isinstance(texto, dict):
        cb = texto.get("callback") or {}
        if notif.config and str(cb.get("data", "")).startswith("cfg|"):
            log.info("Botão: %s", cb["data"])
            try:
                notif.config.on_callback(cb.get("id"), cb.get("message_id"), cb["data"][4:],
                                         bool(cb.get("respondido")))
            except Exception:
                log.exception("Erro no assistente /config")
                notif.responder_cb(cb.get("id"), "Erro no menu. Mande /config de novo.")
        return
    if not texto:
        return
    partes = texto.lstrip("/").split(maxsplit=1)
    cmd = partes[0].lower().split("@")[0] if partes else ""
    arg = partes[1] if len(partes) > 1 else ""
    log.info("Comando recebido: %s %s", cmd, arg)
    if cmd == "pausar":
        seg = duracao(arg)
        if seg is None:
            notif.enviar("Não entendi a duração. Exemplos: /pausar, /pausar 30m, "
                         "/pausar 2h, /pausar 1d", forcar=True)
        else:
            notif.pausar(seg)
    elif cmd == "retomar":
        notif.retomar()
    elif cmd in ("config", "configuracao", "configuração", "configuracoes", "configurações"):
        notif.config.abrir()
    elif cmd == "agenda":
        notif.enviar(notif.agenda.cmd_agenda(arg), forcar=True)
    elif cmd in ("frequencia", "frequência", "freq"):
        notif.enviar(notif.agenda.cmd_freq(arg), forcar=True)
    elif cmd == "regime":
        notif.enviar(regime_txt(cache.get("regime")), forcar=True)
    elif cmd in ("tendencia", "tendência"):
        alvo = arg.split()[0] if arg.strip() else (cfg["simbolos_extras"] or ["BTC"])[0]
        try:
            notif.enviar(analisar_tendencia(client, cfg, alvo, cache), forcar=True)
        except Exception as e:
            log.exception("Erro no /tendencia")
            notif.enviar(f"⚠️ Erro ao calcular a tendência: {type(e).__name__}", forcar=True)
    elif cmd == "status":
        try:
            notif.enviar(status_txt(client, cfg, notif, cache, ordens), forcar=True)
        except Exception as e:
            log.exception("Erro no /status")
            notif.enviar(f"⚠️ Erro ao montar o status: {type(e).__name__}", forcar=True)
    elif cmd in ("ajuda", "help", "start"):
        tema = SINONIMOS_AJUDA.get(arg.strip().lower(), arg.strip().lower())
        for t in ([tema] if tema in AJUDA else ORDEM_AJUDA):
            notif.enviar(AJUDA[t], forcar=True)
    else:
        notif.enviar("Comandos do monitor: /status, /config, /tendencia [moeda], /regime, /agenda, /frequencia, "
                     "/pausar [30m|2h|1d], "
                     "/retomar, /ajuda",
                     forcar=True)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default=str(APP_DIR / "config.json"))
    ap.add_argument("--once", action="store_true", help="um ciclo e sai")
    ap.add_argument("--demo", action="store_true", help="simulação offline")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        handlers=[logging.StreamHandler(),
                                  RotatingFileHandler(APP_DIR / "monitor.log", maxBytes=5_000_000,
                                                      backupCount=3, encoding="utf-8")])
    load_env(APP_DIR / ".env")
    cfg = load_config(Path(args.config))

    if args.demo:
        return demo(cfg)

    key, secret = os.environ.get("BINANCE_API_KEY"), os.environ.get("BINANCE_API_SECRET")
    if cfg["usar_carteira"] and not (key and secret):
        log.warning("Sem BINANCE_API_KEY/SECRET: monitorando só 'simbolos_extras'")
    client = Binance(key, secret)
    state_path = APP_DIR / "estado.json"
    state = load_state(state_path)
    notif = Notificador(cfg, state)
    notif.agenda = agenda = Agenda(cfg, state)
    cache = {"validos": client.valid_symbols(cfg["moeda_cotacao"])}
    notif.config = Configurador(cfg, agenda, notif, cache)
    log.info("Monitor iniciado (%s)", ", ".join(cfg["intervalos"]))

    ordens = MonitorOrdens(client, cfg, state, notif, cache) \
        if cfg["monitorar_ordens"] and client.secret else None
    pasta = Path(cfg["pasta_comandos"] or APP_DIR / "comandos")
    try:
        pasta.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        log.warning("Pasta de comandos indisponível (%s): %s", pasta, e)
    log.info("Lendo comandos em %s%s", pasta,
             " e no Telegram" if notif.ler_comandos else "")
    notif.registrar_menu()
    if cfg["regime"] and not args.once:
        reg = Regime(cfg, cache)
        threading.Thread(target=reg.rodar, name="regime", daemon=True).start()
        log.info("Camada de regime ativa (notícias a cada %s min, regime a cada %s h)",
                 cfg["noticias_min"], cfg["regime_horas"])
    receptor = None
    if not args.once:
        receptor = Receptor(pasta, notif)
        threading.Thread(target=receptor.rodar, name="comandos", daemon=True).start()
        cache["drenar"] = lambda: receptor.drenar(client, cfg, cache, ordens)
    ultimo_ciclo = ultima_ordem = 0
    estava_dentro = agenda.dentro()
    while True:
        inicio = time.time()
        if receptor:
            rodou = receptor.drenar(client, cfg, cache, ordens) > 0
        else:
            rodou = processar_comandos(pasta, client, cfg, notif, cache, ordens)
        mudou = cache.pop("regime_mudou", None)
        if mudou:
            notif.enviar(f"🌡️ Mudança de regime: {mudou[0]} → {mudou[1]}\n\n" +
                         regime_txt(cache.get("regime")))
        if ordens and inicio - ultima_ordem >= cfg["intervalo_ordens_s"]:
            ultima_ordem = rodou = inicio
            try:
                ordens.verificar()
            except BinanceErro as e:
                log.error("Falha ao ler ordens: %s", e)
            except Exception:
                log.exception("Erro ao verificar ordens")
        if inicio - ultimo_ciclo >= cfg["intervalo_verificacao_s"]:
            ultimo_ciclo = rodou = inicio
            try:
                ciclo(client, cfg, state, notif, cache)
            except Exception:
                log.exception("Erro no ciclo")
        # Abertura/fechamento do horário de atividade
        dentro = agenda.dentro()
        if dentro != estava_dentro:
            estava_dentro = rodou = dentro
            if dentro:
                fora = state.pop("fora", None) or {"suprimidos": 0, "fila": []}
                txt = (f"☀️ Início do horário de atividade ({agenda.janela_txt()}). "
                       f"Fora do horário: {fora['suprimidos']} alerta(s) de mercado não enviados.")
                if fora["fila"]:
                    txt += "\n\nAvisos guardados:\n\n" + "\n\n".join(fora["fila"])
                notif.enviar(txt, forcar=True)
            else:
                notif.enviar("🌙 Fim do horário de atividade. Alertas ficam guardados até a "
                             "próxima abertura" + ("; avisos de execução de ordens continuam "
                             "chegando na hora." if agenda.st["ordens_sempre"] else "."),
                             forcar=True)

        # Relatórios programados (/frequencia)
        if cache.get("resumo_linhas") and agenda.vencido("analise"):
            rodou = True
            notif.enviar("📊 Análise do monitor\n" + "\n".join(cache["resumo_linhas"]))
        if cache.get("resumo_linhas") and agenda.vencido("sugestao"):
            rodou = True
            for sym in agenda.simbolos_sugestao():
                try:
                    notif.enviar(analisar_tendencia(client, cfg, sym, cache))
                except Exception:
                    log.exception("Erro na sugestão automática de %s", sym)
        if agenda.vencido("regime"):
            rodou = True
            if cfg["regime"] and not args.once:
                cache["regime_enviar"] = cache["regime_forcar"] = True
            else:
                notif.enviar(regime_txt(cache.get("regime")))
        if cache.pop("regime_pronto", False):
            notif.enviar(regime_txt(cache.get("regime")))
        if rodou:
            state["tg_offset"] = notif.tg_offset
            save_state(state_path, state)
        if args.once:
            break
        time.sleep(0.5)                     # comandos e cliques executam em até ~0,5 s


# --------------------------------------------------------------------------
# Simulação offline: faixa lateral → pressão → rompimento → reteste
# --------------------------------------------------------------------------
def demo(cfg):
    random.seed(7)
    cs, p, t = [], 100.0, 0
    def vela(alvo, ruido, vol):
        nonlocal p, t
        o = p
        c = alvo + random.uniform(-ruido, ruido)
        h = max(o, c) + random.uniform(0, ruido)
        l = min(o, c) - random.uniform(0, ruido)
        cs.append(Candle(t, o, h, l, c, vol * random.uniform(0.8, 1.2), t + 1))
        p, t = c, t + 2
    for i in range(160):                           # faixa 100–110
        vela(105 + 4.5 * math.sin(i / 6), 0.8, 100)
    for i in range(14):                            # fundos subindo contra 110
        vela(107.5 + i * 0.18 + 1.2 * math.sin(i), 0.35, 120)
    vela(112.3, 0.2, 260)                          # rompimento com volume
    for alvo in (113, 112.4, 111.2, 110.7, 111.4, 112.2):   # reteste e acomodação
        vela(alvo, 0.3, 110)
    st, enviados = {}, set()
    for n in range(100, len(cs) + 1):
        _, sinais = analyze(cs[:n], cs[n - 1].c, cfg, st)
        for s in sinais:
            if s.chave not in enviados:
                enviados.add(s.chave)
                print(f"\n[vela {n}] 🔔 {s.titulo}\n  " + "\n  ".join(s.linhas))


if __name__ == "__main__":
    main()
