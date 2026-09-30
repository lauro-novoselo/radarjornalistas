#!/usr/bin/env python3
"""
radar_jornalistas.py — Banco de jornalistas + recomendação de pautas

Uso:
  pip install requests beautifulsoup4 scikit-learn

  # 1) Descobrir jornalistas pelos sitemaps de notícias dos veículos
  python radar_jornalistas.py descobrir --veiculos folha estadao oglobo --max 150

  # 2) Coletar o histórico de um autor específico (URL da página de autor)
  python radar_jornalistas.py autor https://www1.folha.uol.com.br/autores/cristiane-gercina.shtml

  # 3) Aprofundar todos os jornalistas com poucas matérias no banco
  python radar_jornalistas.py aprofundar --min 5 --max 25

  # 4) Recomendar jornalistas para uma pauta
  python radar_jornalistas.py pauta "Fintech lança crédito consignado para aposentados do INSS"
  python radar_jornalistas.py pauta --arquivo release.txt --top 15 --dias 120

  # 5) Rotina diária (é este comando que você agenda)
  python radar_jornalistas.py diario

  # 6) Acervo de títulos e temas de um jornalista
  python radar_jornalistas.py acervo "Cristiane Gercina" --csv acervo_cristiane.csv

  # 7) Exportar a base para planilha
  python radar_jornalistas.py exportar jornalistas.csv
"""
import argparse
import csv
import gzip
import html
import json
import math
import re
import sqlite3
import sys
import time
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from datetime import date, datetime
from urllib import robotparser
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

DB = "jornalistas.db"
UA = "RadarJornalistas/1.0 (+contato: seu-email@suaagencia.com.br)"  # coloque um contato real
PAUSA = 2.0  # segundos entre requisições ao mesmo site

VEICULOS = {
    "folha": "https://www1.folha.uol.com.br",
    "estadao": "https://www.estadao.com.br",
    "oglobo": "https://oglobo.globo.com",
    "g1": "https://g1.globo.com",
    "valor": "https://valor.globo.com",
    "uol": "https://noticias.uol.com.br",
    "cnnbrasil": "https://www.cnnbrasil.com.br",
    "metropoles": "https://www.metropoles.com",
    "exame": "https://exame.com",
    "infomoney": "https://www.infomoney.com.br",
}

# Assinaturas genéricas que não são jornalistas
IGNORAR = {
    "redação", "redacao", "da redação", "reuters", "afp", "ap", "associated press",
    "bloomberg", "estadão conteúdo", "folhapress", "agência brasil", "agência o globo",
    "o globo", "g1", "uol", "cnn brasil", "folha", "estadão", "valor", "exame",
}

TIPOS_MATERIA = {
    "NewsArticle", "Article", "ReportageNewsArticle", "AnalysisNewsArticle",
    "OpinionNewsArticle", "BlogPosting", "LiveBlogPosting", "ReviewNewsArticle",
}

STOPWORDS_PT = """a ao aos aquela aquelas aquele aqueles aquilo as ate com como da das de dela
delas dele deles depois do dos e ela elas ele eles em entre era essa essas esse esses esta estas
este estes eu foi foram ha isso isto ja lhe mais mas me mesmo meu minha muito na nas nem no nos
nossa nosso num numa o os ou para pela pelas pelo pelos por qual quando que quem se sem ser seu
sua suas seus so tambem te tem ter um uma umas uns voce voces vai sobre apos diz afirma segundo
ano anos novo nova pode podem deve devem ainda ate sao esta estao foi sera serao""".split()


def log(msg):
    print(msg, file=sys.stderr, flush=True)


def limpa(t):
    if not t:
        return ""
    if isinstance(t, (list, dict)):
        t = json.dumps(t, ensure_ascii=False)
    return re.sub(r"\s+", " ", html.unescape(str(t))).strip()


def raiz(url):
    p = urlparse(url)
    return f"{p.scheme}://{p.netloc}"


def dominio(url):
    partes = urlparse(url).netloc.lower().split(".")
    return ".".join(partes[-3:] if partes[-1] == "br" else partes[-2:])


def veiculo_de(url):
    host = urlparse(url).netloc.lower()
    for chave, base in VEICULOS.items():
        if urlparse(base).netloc.lower() == host:
            return chave
    return host


# --------------------------------------------------------------------------- HTTP

class Cliente:
    def __init__(self, pausa=PAUSA):
        self.s = requests.Session()
        self.s.headers.update({"User-Agent": UA, "Accept-Language": "pt-BR,pt;q=0.9"})
        self.pausa = pausa
        self.robots = {}
        self.ultimo = defaultdict(float)

    def _robots(self, url):
        base = raiz(url)
        if base not in self.robots:
            rp = robotparser.RobotFileParser()
            try:
                r = self.s.get(base + "/robots.txt", timeout=15)
                rp.parse(r.text.splitlines() if r.ok else [])
            except requests.RequestException:
                rp.parse([])
            self.robots[base] = rp
        return self.robots[base]

    def permitido(self, url):
        return self._robots(url).can_fetch(UA, url)

    def sitemaps(self, url):
        return list(self._robots(url).site_maps() or [])

    def get(self, url):
        if not self.permitido(url):
            log(f"  [robots.txt bloqueia] {url}")
            return None
        host = urlparse(url).netloc
        espera = self.pausa - (time.time() - self.ultimo[host])
        if espera > 0:
            time.sleep(espera)
        try:
            r = self.s.get(url, timeout=20)
            self.ultimo[host] = time.time()
            if r.status_code == 200:
                return r
            log(f"  [HTTP {r.status_code}] {url}")
        except requests.RequestException as e:
            log(f"  [erro {e.__class__.__name__}] {url}")
        return None


# --------------------------------------------------------------------------- Banco

def conectar():
    con = sqlite3.connect(DB)
    con.executescript("""
    CREATE TABLE IF NOT EXISTS jornalistas(
        id INTEGER PRIMARY KEY,
        nome TEXT NOT NULL,
        veiculo TEXT NOT NULL,
        url_autor TEXT,
        atualizado TEXT,
        UNIQUE(veiculo, nome));
    CREATE TABLE IF NOT EXISTS materias(
        url TEXT NOT NULL,
        jornalista_id INTEGER NOT NULL REFERENCES jornalistas(id),
        titulo TEXT, resumo TEXT, palavras_chave TEXT, secao TEXT, data TEXT,
        coletado_em TEXT,
        PRIMARY KEY(url, jornalista_id));
    CREATE TABLE IF NOT EXISTS execucoes(
        inicio TEXT, fim TEXT, comando TEXT, materias_novas INTEGER, jornalistas_novos INTEGER);
    CREATE INDEX IF NOT EXISTS idx_materias_url ON materias(url);
    CREATE INDEX IF NOT EXISTS idx_materias_data ON materias(data);
    """)
    cols = [c[1] for c in con.execute("PRAGMA table_info(materias)")]
    if "coletado_em" not in cols:
        con.execute("ALTER TABLE materias ADD COLUMN coletado_em TEXT")
    return con


def ja_coletada(con, url):
    return con.execute("SELECT 1 FROM materias WHERE url=? LIMIT 1", (url,)).fetchone() is not None


def contar(con):
    return (con.execute("SELECT COUNT(*) FROM materias").fetchone()[0],
            con.execute("SELECT COUNT(*) FROM jornalistas").fetchone()[0])


def salvar_jornalista(con, nome, veiculo, url_autor=""):
    con.execute(
        """INSERT INTO jornalistas(nome, veiculo, url_autor, atualizado) VALUES(?,?,?,?)
           ON CONFLICT(veiculo, nome) DO UPDATE SET
             url_autor = COALESCE(NULLIF(excluded.url_autor,''), jornalistas.url_autor),
             atualizado = excluded.atualizado""",
        (nome, veiculo, url_autor, datetime.now().isoformat(timespec="seconds")))
    return con.execute("SELECT id FROM jornalistas WHERE veiculo=? AND nome=?",
                       (veiculo, nome)).fetchone()[0]


def salvar_materia(con, jid, m):
    con.execute(
        """INSERT INTO materias(url, jornalista_id, titulo, resumo, palavras_chave, secao, data, coletado_em)
           VALUES(?,?,?,?,?,?,?,?)
           ON CONFLICT(url, jornalista_id) DO UPDATE SET
             titulo=excluded.titulo, resumo=excluded.resumo, palavras_chave=excluded.palavras_chave,
             secao=excluded.secao, data=excluded.data""",
        (m["url"], jid, m["titulo"], m["resumo"], m["palavras_chave"], m["secao"], m["data"],
         datetime.now().isoformat(timespec="seconds")))


# --------------------------------------------------------------------------- Extração

def blocos_jsonld(soup):
    itens = []
    for tag in soup.find_all("script", type="application/ld+json"):
        try:
            dado = json.loads(tag.string or tag.get_text())
        except (json.JSONDecodeError, TypeError):
            continue
        pilha = [dado]
        while pilha:
            x = pilha.pop()
            if isinstance(x, list):
                pilha.extend(x)
            elif isinstance(x, dict):
                g = x.get("@graph")
                if g:
                    pilha.extend(g if isinstance(g, list) else [g])
                itens.append(x)
    return itens


def eh_materia(x):
    t = x.get("@type")
    return any(i in TIPOS_MATERIA for i in (t if isinstance(t, list) else [t]))


def meta(soup, *nomes):
    for n in nomes:
        tag = soup.find("meta", attrs={"property": n}) or soup.find("meta", attrs={"name": n})
        if tag and tag.get("content"):
            return tag["content"].strip()
    return ""


def autor_valido(nome):
    n = nome.lower().strip()
    return bool(n) and n not in IGNORAR and "redação" not in n and not n.startswith("agência")


def ler_materia(cli, url):
    r = cli.get(url)
    if not r:
        return None
    soup = BeautifulSoup(r.content, "html.parser")
    art = next((x for x in blocos_jsonld(soup) if eh_materia(x)), {})

    autores = []
    bruto = art.get("author", [])
    for p in (bruto if isinstance(bruto, list) else [bruto]):
        if isinstance(p, dict) and p.get("name"):
            u = p.get("url") or ""
            if isinstance(u, list):
                u = u[0] if u else ""
            autores.append((limpa(p["name"]), urljoin(url, u) if u else ""))
        elif isinstance(p, str):
            autores.append((limpa(p), ""))
    if not autores:
        n = meta(soup, "author", "article:author")
        if n and not n.startswith("http"):
            autores.append((limpa(n), ""))
    autores = [(n, u) for n, u in autores if autor_valido(n)]

    kw = art.get("keywords") or meta(soup, "news_keywords", "keywords")
    if isinstance(kw, list):
        kw = ", ".join(map(str, kw))
    secao = art.get("articleSection") or meta(soup, "article:section")
    if isinstance(secao, list):
        secao = ", ".join(map(str, secao))
    data = art.get("datePublished") or meta(soup, "article:published_time") or ""

    return {
        "url": url,
        "titulo": limpa(art.get("headline") or meta(soup, "og:title")
                        or (soup.title.string if soup.title else "")),
        "resumo": limpa(art.get("description") or meta(soup, "og:description", "description")),
        "palavras_chave": limpa(kw),
        "secao": limpa(secao),
        "data": str(data)[:10],
        "autores": autores,
    }


PADRAO_MATERIA = re.compile(r"/20\d\d/|\.s?html?$|\.ghtml$|-\d{6,}")


def links_de_materia(conteudo, base):
    soup = BeautifulSoup(conteudo, "html.parser")
    dom, vistos = dominio(base), []
    for a in soup.find_all("a", href=True):
        u = urljoin(base, a["href"]).split("#")[0].split("?")[0]
        caminho = urlparse(u).path
        if dominio(u) != dom or "/autor" in caminho:
            continue
        slug = caminho.rstrip("/").rsplit("/", 1)[-1]
        if PADRAO_MATERIA.search(caminho) and slug.count("-") >= 3 and u not in vistos:
            vistos.append(u)
    return vistos


def ler_xml(r):
    dados = r.content
    if dados[:2] == b"\x1f\x8b":
        dados = gzip.decompress(dados)
    return ET.fromstring(dados)


def urls_do_sitemap(cli, url, limite, prof=0):
    r = cli.get(url)
    if not r:
        return []
    try:
        raiz_xml = ler_xml(r)
    except (ET.ParseError, OSError):
        return []
    locs = [filho.text.strip() for el in raiz_xml for filho in el
            if filho.tag.split("}")[-1] == "loc" and filho.text]
    if raiz_xml.tag.endswith("sitemapindex"):
        filhos = sorted([l for l in locs if "news" in l.lower()] or locs, reverse=True)
        saida = []
        for f in filhos[:5]:
            if prof < 2:
                saida += urls_do_sitemap(cli, f, limite - len(saida), prof + 1)
            if len(saida) >= limite:
                break
        return saida[:limite]
    return locs[:limite]


# --------------------------------------------------------------------------- Comandos

def cmd_descobrir(args):
    cli, con = Cliente(args.pausa), conectar()
    for chave in args.veiculos:
        base = VEICULOS.get(chave, chave if chave.startswith("http") else None)
        if not base:
            log(f"Veículo desconhecido: {chave}")
            continue
        sms = cli.sitemaps(base + "/") or [base + "/sitemap.xml"]
        sms = [s for s in sms if "news" in s.lower() or "noticia" in s.lower()] or sms
        log(f"\n== {chave}: {len(sms)} sitemap(s)")
        urls = []
        for sm in sms:
            urls += urls_do_sitemap(cli, sm, args.max - len(urls))
            if len(urls) >= args.max:
                break
        urls = [u for u in urls if not ja_coletada(con, u)]
        log(f"   {len(urls)} matérias novas para ler")
        novos = Counter()
        for i, u in enumerate(urls, 1):
            m = ler_materia(cli, u)
            if not m:
                continue
            for nome, url_autor in m["autores"]:
                jid = salvar_jornalista(con, nome, veiculo_de(u), url_autor)
                salvar_materia(con, jid, m)
                novos[nome] += 1
            if i % 20 == 0:
                con.commit()
                log(f"   {i}/{len(urls)} lidas, {len(novos)} autores")
        con.commit()
        log(f"   {chave}: {len(novos)} jornalistas encontrados")


def coletar_autor(cli, con, url_autor, maximo):
    r = cli.get(url_autor)
    if not r:
        return 0
    links = [u for u in links_de_materia(r.content, url_autor)[:maximo] if not ja_coletada(con, u)]
    log(f"\n== {url_autor}: {len(links)} matérias novas")
    materias, contagem, por_url = [], Counter(), {}
    for u in links:
        m = ler_materia(cli, u)
        if not m:
            continue
        materias.append(m)
        for nome, ua in m["autores"]:
            contagem[nome] += 1
            if ua:
                por_url[ua.rstrip("/")] = nome
    nome = por_url.get(url_autor.rstrip("/"))
    if not nome and contagem:
        nome = contagem.most_common(1)[0][0]
    if not nome:
        soup = BeautifulSoup(r.content, "html.parser")
        h1 = soup.find("h1")
        nome = limpa(h1.get_text()) if h1 else ""
    if not nome:
        log("   não consegui identificar o nome do autor")
        return 0
    jid = salvar_jornalista(con, nome, veiculo_de(url_autor), url_autor)
    n = 0
    for m in materias:
        if not m["autores"] or nome in [a for a, _ in m["autores"]]:
            salvar_materia(con, jid, m)
            n += 1
    con.commit()
    log(f"   {nome}: {n} matérias salvas")
    return n


def cmd_autor(args):
    cli, con = Cliente(args.pausa), conectar()
    for u in args.urls:
        coletar_autor(cli, con, u, args.max)


def cmd_aprofundar(args):
    cli, con = Cliente(args.pausa), conectar()
    alvos = con.execute("""
        SELECT j.url_autor FROM jornalistas j LEFT JOIN materias m ON m.jornalista_id=j.id
        WHERE COALESCE(j.url_autor,'') <> ''
        GROUP BY j.id HAVING COUNT(m.url) < ?""", (args.min,)).fetchall()
    log(f"{len(alvos)} jornalistas para aprofundar")
    for (u,) in alvos:
        coletar_autor(cli, con, u, args.max)


def cmd_pauta(args):
    from sklearn.feature_extraction.text import TfidfVectorizer
    from sklearn.metrics.pairwise import linear_kernel

    texto = open(args.arquivo, encoding="utf-8").read() if args.arquivo else " ".join(args.texto)
    if not texto.strip():
        sys.exit("Informe o texto da pauta ou --arquivo.")
    con = conectar()
    sql = """SELECT j.id, j.nome, j.veiculo, j.url_autor, m.titulo, m.resumo,
                    m.palavras_chave, m.secao, m.data, m.url
             FROM materias m JOIN jornalistas j ON j.id = m.jornalista_id"""
    params = ()
    if args.veiculo:
        sql += " WHERE j.veiculo IN (%s)" % ",".join("?" * len(args.veiculo))
        params = tuple(args.veiculo)
    linhas = con.execute(sql, params).fetchall()
    if not linhas:
        sys.exit("Banco vazio. Rode 'descobrir' ou 'autor' primeiro.")

    docs = [f"{t} {t} {r} {k} {k} {s}" for _, _, _, _, t, r, k, s, _, _ in linhas]
    vet = TfidfVectorizer(strip_accents="unicode", lowercase=True, stop_words=STOPWORDS_PT,
                          ngram_range=(1, 2), sublinear_tf=True, min_df=1, max_df=0.5)
    matriz = vet.fit_transform(docs + [texto])
    sims = linear_kernel(matriz[-1], matriz[:-1]).ravel()

    hoje = date.today()
    por_jornalista = defaultdict(list)
    for (jid, nome, veic, ua, tit, _, _, _, dt, url), s in zip(linhas, sims):
        try:
            idade = (hoje - date.fromisoformat(dt)).days
        except ValueError:
            idade = args.dias
        peso = math.exp(-max(idade, 0) / args.dias)
        por_jornalista[(jid, nome, veic, ua)].append((s * peso, s, tit, dt, url))

    ranking = []
    for chave, mats in por_jornalista.items():
        mats.sort(reverse=True)
        nota = sum(m[0] for m in mats[:3])
        if nota > 0:
            ranking.append((nota, chave, mats[:2], len(mats)))
    ranking.sort(reverse=True, key=lambda x: x[0])

    print(f"\nPauta: {texto[:120]}{'...' if len(texto) > 120 else ''}\n")
    for pos, (nota, (_, nome, veic, ua), mats, total) in enumerate(ranking[:args.top], 1):
        print(f"{pos:2d}. {nome} — {veic}  (aderência {nota:.3f}, {total} matérias no banco)")
        if ua:
            print(f"    {ua}")
        for _, s, tit, dt, url in mats:
            print(f"    • [{dt}] {tit[:100]}  (sim {s:.2f})")
            print(f"      {url}")
        print()


def cmd_exportar(args):
    con = conectar()
    linhas = con.execute("""
        SELECT j.nome, j.veiculo, j.url_autor, COUNT(m.url), MAX(m.data),
               GROUP_CONCAT(DISTINCT m.secao)
        FROM jornalistas j LEFT JOIN materias m ON m.jornalista_id=j.id
        GROUP BY j.id ORDER BY j.veiculo, j.nome""").fetchall()
    with open(args.saida, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(["nome", "veiculo", "url_autor", "materias", "ultima_materia", "editorias"])
        w.writerows(linhas)
    log(f"{len(linhas)} jornalistas exportados para {args.saida}")


def cmd_diario(args):
    """Rotina diária: sitemaps de todos os veículos + páginas de autores ativos."""
    inicio = datetime.now().isoformat(timespec="seconds")
    con = conectar()
    m0, j0 = contar(con)
    cmd_descobrir(argparse.Namespace(veiculos=args.veiculos, max=args.max, pausa=args.pausa))
    cli = Cliente(args.pausa)
    ativos = con.execute("""
        SELECT j.url_autor FROM jornalistas j JOIN materias m ON m.jornalista_id=j.id
        WHERE COALESCE(j.url_autor,'') <> '' AND m.data >= date('now', ?)
        GROUP BY j.id ORDER BY MAX(m.data) DESC LIMIT ?""",
        (f"-{args.ativos_dias} days", args.max_autores)).fetchall()
    log(f"\n{len(ativos)} páginas de autores ativos para revisitar")
    for (u,) in ativos:
        coletar_autor(cli, con, u, args.por_autor)
    m1, j1 = contar(con)
    con.execute("INSERT INTO execucoes VALUES(?,?,?,?,?)",
                (inicio, datetime.now().isoformat(timespec="seconds"), "diario", m1 - m0, j1 - j0))
    con.commit()
    log(f"\nFim: +{m1 - m0} matérias, +{j1 - j0} jornalistas (total {m1} / {j1})")


def temas_de(linhas):
    c = Counter()
    for kw, secao in linhas:
        for t in re.split(r"[,;|]", kw or ""):
            t = t.strip()
            if 2 < len(t) < 60:
                c[t.lower()] += 1
        for t in re.split(r"[,;|]", secao or ""):
            if t.strip():
                c["[editoria] " + t.strip().lower()] += 1
    return c


def cmd_acervo(args):
    con = conectar()
    js = con.execute("SELECT id, nome, veiculo, url_autor FROM jornalistas WHERE nome LIKE ? ORDER BY nome",
                     (f"%{args.nome}%",)).fetchall()
    if not js:
        sys.exit(f"Nenhum jornalista com '{args.nome}' no nome.")
    saida = []
    for jid, nome, veic, ua in js:
        mats = con.execute("""SELECT data, titulo, palavras_chave, secao, url FROM materias
                              WHERE jornalista_id=? ORDER BY data DESC""", (jid,)).fetchall()
        datas = [m[0] for m in mats if m[0]]
        print(f"\n{'=' * 70}\n{nome} — {veic}\n{ua or ''}")
        print(f"{len(mats)} matérias no acervo"
              + (f", de {min(datas)} a {max(datas)}" if datas else ""))
        print("\nTemas mais frequentes:")
        for t, n in temas_de([(m[2], m[3]) for m in mats]).most_common(args.temas):
            print(f"  {n:4d}  {t}")
        print("\nTítulos por mês:")
        mes_atual = None
        for dt, tit, kw, sec, url in mats[:args.titulos]:
            mes = (dt or "sem data")[:7]
            if mes != mes_atual:
                print(f"  -- {mes}")
                mes_atual = mes
            print(f"     [{dt}] {tit}")
            saida.append([nome, veic, dt, tit, sec, kw, url])
    if args.csv:
        with open(args.csv, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f, delimiter=";")
            w.writerow(["jornalista", "veiculo", "data", "titulo", "editoria", "palavras_chave", "url"])
            w.writerows(saida)
        log(f"\nAcervo exportado para {args.csv}")


def main():
    ap = argparse.ArgumentParser(description="Banco de jornalistas e recomendação de pautas")
    ap.add_argument("--pausa", type=float, default=PAUSA, help="segundos entre requisições")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("descobrir", help="acha jornalistas via sitemaps de notícias")
    p.add_argument("--veiculos", nargs="+", default=list(VEICULOS))
    p.add_argument("--max", type=int, default=150, help="matérias por veículo")
    p.set_defaults(f=cmd_descobrir)

    p = sub.add_parser("autor", help="coleta o histórico de páginas de autor")
    p.add_argument("urls", nargs="+")
    p.add_argument("--max", type=int, default=30)
    p.set_defaults(f=cmd_autor)

    p = sub.add_parser("aprofundar", help="coleta histórico de quem tem poucas matérias")
    p.add_argument("--min", type=int, default=5)
    p.add_argument("--max", type=int, default=25)
    p.set_defaults(f=cmd_aprofundar)

    p = sub.add_parser("pauta", help="recomenda jornalistas para uma pauta")
    p.add_argument("texto", nargs="*")
    p.add_argument("--arquivo")
    p.add_argument("--top", type=int, default=10)
    p.add_argument("--dias", type=int, default=180, help="meia-vida do peso de recência")
    p.add_argument("--veiculo", nargs="+", help="filtrar por veículo(s)")
    p.set_defaults(f=cmd_pauta)

    p = sub.add_parser("diario", help="rotina diária completa (para agendar)")
    p.add_argument("--veiculos", nargs="+", default=list(VEICULOS))
    p.add_argument("--max", type=int, default=200, help="matérias do sitemap por veículo")
    p.add_argument("--ativos-dias", type=int, default=30, help="revisita autores ativos nesse período")
    p.add_argument("--max-autores", type=int, default=150)
    p.add_argument("--por-autor", type=int, default=15)
    p.set_defaults(f=cmd_diario)

    p = sub.add_parser("acervo", help="histórico de títulos e temas de um jornalista")
    p.add_argument("nome", help="nome ou parte do nome")
    p.add_argument("--temas", type=int, default=15)
    p.add_argument("--titulos", type=int, default=100)
    p.add_argument("--csv", help="exporta os títulos para CSV")
    p.set_defaults(f=cmd_acervo)

    p = sub.add_parser("exportar", help="exporta a base para CSV (abre no Excel)")
    p.add_argument("saida", nargs="?", default="jornalistas.csv")
    p.set_defaults(f=cmd_exportar)

    args = ap.parse_args()
    args.f(args)


if __name__ == "__main__":
    main()
