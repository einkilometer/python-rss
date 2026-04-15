import sqlite3
import feedparser
import requests
import re
import threading
from datetime import datetime
from urllib.parse import urljoin
from bs4 import BeautifulSoup
from flask import Flask, render_template_string, request, redirect, url_for, jsonify, flash

app = Flask(__name__)
app.secret_key = 'newsraft_secret'
DATABASE = 'newsraft_man.db'

progress_tracker = {"current": 0, "total": 0, "status": "idle"}

# --- HELPER LOGIC ---

def strip_protocol(url):
    if not url: return "N/A"
    return re.sub(r'^https?://', '', url)

def get_url_slug(url):
    if not url: return "link"
    path = url.rstrip('/')
    last_part = path.split('/')[-1]
    slug = last_part[:3] if len(last_part) >= 3 else last_part
    return slug if slug else "lk"

def format_date(date_str):
    if not date_str or date_str == "No date":
        return "Unknown Date"
    try:
        clean_date = re.sub(r'\s+', ' ', date_str).strip()
        for fmt in ("%a, %d %b %Y %H:%M:%S %Z", "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%d %H:%M:%S"):
            try:
                return datetime.strptime(clean_date, fmt).strftime('%Y-%m-%d %H:%M')
            except ValueError:
                continue
        return clean_date[:16]
    except Exception:
        return date_str

# --- DATABASE LOGIC ---

def get_db():
    conn = sqlite3.connect(DATABASE, timeout=20)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with get_db() as conn:
        conn.execute('PRAGMA journal_mode=WAL;')
        conn.execute('''CREATE TABLE IF NOT EXISTS feeds
                        (id INTEGER PRIMARY KEY AUTOINCREMENT, url TEXT UNIQUE)''')
        conn.execute('''CREATE TABLE IF NOT EXISTS articles
                        (id INTEGER PRIMARY KEY AUTOINCREMENT, feed_id INTEGER,
                         title TEXT, link TEXT, pub_date TEXT, content TEXT,
                         FOREIGN KEY(feed_id) REFERENCES feeds(id))''')
        conn.commit()

def add_feed(url):
    try:
        with get_db() as conn:
            conn.execute("INSERT INTO feeds (url) VALUES (?)", (url,))
            conn.commit()
    except sqlite3.IntegrityError:
        pass

# --- ON-DEMAND SCRAPER ---

def scrape_article_content(url):
    try:
        headers = {'User-Agent': 'Mozilla/5.0 (Linux; x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'}
        response = requests.get(url, headers=headers, timeout=10)
        response.raise_for_status()
        soup = BeautifulSoup(response.content, 'html.parser')
        for noise in soup(["script", "style", "nav", "footer", "header", "aside"]):
            noise.decompose()

        url_map = {}
        ref_order = []
        for tag in soup.find_all(['a', 'img']):
            link = tag.get('href') or tag.get('src')
            if link:
                absolute_url = urljoin(url, link)
                short_url = absolute_url.split('?')[0].split('#')[0]
                if short_url not in url_map:
                    ref_id = len(url_map) + 1
                    slug = get_url_slug(short_url)
                    url_map[short_url] = (ref_id, slug)
                    ref_order.append(short_url)
                current_id = url_map[short_url][0]
                tag.string = f"[[REF:{current_id}]]"

        blocks = []
        for element in soup.find_all(['p', 'div', 'h1', 'h2', 'h3', 'li']):
            if element.name == 'div' and element.find_all(['p', 'div']):
                continue
            text = element.get_text().strip()
            if text:
                blocks.append(" ".join(text.split()))

        clean_text = "\n\n".join(blocks)
        if not clean_text:
            raw_text = soup.get_text()
            clean_text = " ".join(raw_text.split())

        while True:
            new_text = re.sub(r'(\[\[REF:\d+\]\])[\s\n\r\t\xa0]*(\[\[REF:\d+\]\])', r'\1\2', clean_text)
            if new_text == clean_text: break
            clean_text = new_text

        for short_url, (ref_id, slug) in url_map.items():
            marker = f"[[REF:{ref_id}]]"
            html_link = f'<a class="ref-link" href="{short_url}" target="_blank" title="{short_url}">[{slug}{ref_id}]</a>'
            clean_text = clean_text.replace(marker, html_link)

        if url_map:
            ref_entries = [f"[{url_map[u][1]}{url_map[u][0]}] {u}" for u in ref_order]
            clean_text += "\n\n[REFERENCES]\n" + "\n".join(ref_entries)

        return clean_text if clean_text else "Content could not be extracted."
    except Exception as e:
        return f"Error scraping content: {str(e)}"

# --- SYNC LOGIC ---

def sync_feed(feed_id):
    conn = get_db()
    try:
        feed_row = conn.execute("SELECT url FROM feeds WHERE id = ?", (feed_id,)).fetchone()
        if not feed_row: return
        url = feed_row['url']
        feed_data = feedparser.parse(url)
        online_links = []
        for entry in feed_data.entries:
            link = entry.get('link', '#')
            title = entry.get('title', 'No Title')
            raw_date = entry.get('published', entry.get('updated', 'No date'))
            date = format_date(raw_date)
            online_links.append(link)
            existing = conn.execute("SELECT id FROM articles WHERE feed_id = ? AND link = ?", (feed_id, link)).fetchone()
            if existing:
                conn.execute("UPDATE articles SET title = ?, pub_date = ? WHERE id = ?", (title, date, existing['id']))
            else:
                conn.execute("INSERT INTO articles (feed_id, title, link, pub_date, content) VALUES (?, ?, ?, ?, ?)",
                             (feed_id, title, link, date, None))
        if online_links:
            placeholders = ','.join(['?'] * len(online_links))
            conn.execute(f"DELETE FROM articles WHERE feed_id = ? AND link NOT IN ({placeholders})",
                         (feed_id, *online_links))
        conn.commit()
    finally:
        conn.close()

def background_update():
    global progress_tracker
    conn = get_db()
    try:
        feeds = conn.execute("SELECT id, url FROM feeds").fetchall()
        progress_tracker["total"] = len(feeds)
        progress_tracker["current"] = 0
        progress_tracker["status"] = "processing"
        for feed in feeds:
            sync_feed(feed['id'])
            progress_tracker["current"] += 1
        progress_tracker["status"] = "complete"
    finally:
        conn.close()

# --- TEMPLATES ---

INDEX_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <title>RSS Sources</title>
    <style>
        body { font-family: monospace; max-width: 850px; margin: auto; padding: 20px; background: #1a1a1a; color: #00ff00; }
        a { color: #00ff00; text-decoration: none; }
        a:hover { background: #00ff00; color: #1a1a1a; }
        .source-card { border: 1px solid #00ff00; padding: 10px; margin-bottom: 10px; display: flex; justify-content: space-between; align-items: center; }
        form { margin-bottom: 20px; border: 1px dashed #00ff00; padding: 15px; }
        input { background: #000; color: #00ff00; border: 1px solid #00ff00; padding: 5px; }
        button { cursor: pointer; background: #00ff00; color: #000; border: none; padding: 5px 10px; font-weight: bold; }
        #progress-container { display: none; margin: 20px 0; border: 1px solid #00ff00; padding: 10px; }
        #progress-bar { height: 20px; background: #00ff00; width: 0%; transition: width 0.3s; }
        #progress-bg { background: #000; height: 20px; width: 100%; }
        .flash { color: #ff0000; font-weight: bold; margin-bottom: 10px; border: 1px solid #ff0000; padding: 5px; }
    </style>
    <script>
        function checkProgress() {
            fetch('/progress').then(r => r.json()).then(data => {
                if (data.status === 'processing') {
                    document.getElementById('progress-container').style.display = 'block';
                    let percent = Math.round((data.current / data.total) * 100);
                    document.getElementById('progress-bar').style.width = percent + '%';
                    document.getElementById('progress-text').innerText = 'Syncing: ' + data.current + '/' + data.total + ' feeds (' + percent + '%)';
                    setTimeout(checkProgress, 1000);
                } else if (data.status === 'complete') {
                    document.getElementById('progress-text').innerText = 'All Sources Synchronized!';
                    setTimeout(() => { document.getElementById('progress-container').style.display = 'none'; }, 3000);
                }
            });
        }
    </script>
</head>
<body onload="checkProgress()">
    <h1>RSS_SOURCES</h1>
    {% with messages = get_flashed_messages() %}
      {% if messages %}
        {% for msg in messages %}<div class="flash">{{ msg }}</div>{% endfor %}
      {% endif %}
    {% endwith %}
    <form action="/add" method="post">
        <input type="text" name="url" placeholder="RSS_URL" required style="width: 60%;">
        <button type="submit">ADD_FEED</button>
    </form>
    <div id="progress-container">
        <div id="progress-text">Initializing...</div>
        <div id="progress-bg"><div id="progress-bar"></div></div>
    </div>
    <hr>
    {% for feed in feeds %}
    <div class="source-card">
        <a href="/feed/{{ feed.id }}"><strong>[ {{ feed.url }} ]</strong></a>
        <small>ID: {{ feed.id }}</small>
    </div>
    {% endfor %}
</body>
</html>
"""

FEED_VIEW_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <title>Source {{ feed_id }}</title>
    <style>
        body { font-family: monospace; max-width: 1000px; margin: auto; padding: 20px; background: #1a1a1a; color: #00ff00; }
        a { color: #00ff00; text-decoration: none; }
        a:hover { background: #00ff00; color: #1a1a1a; }
        .nav-bar { display: flex; justify-content: space-between; align-items: center; margin-bottom: 20px; }
        .back-link { text-decoration: underline; }
        .sync-btn { background: #00ff00; color: #000; border: none; padding: 5px 10px; font-weight: bold; cursor: pointer; font-family: monospace; }
        .sync-btn:hover { background: #fff; }
        .del-btn { background: #ff0000; color: #fff; border: none; padding: 5px 10px; font-weight: bold; cursor: pointer; font-family: monospace; }
        .del-btn:hover { background: #fff; color: #ff0000; }
        .article-row { display: flex; gap: 15px; padding: 4px 0; border-bottom: 1px solid #333; align-items: center; }
        .article-row:hover { background: #252525; }
        .is-scraped { color: #bfffbf; font-weight: bold; }
        .col-title { flex: 4; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
        .col-external { width: 30px; text-align: center; }
        .col-date { flex: 1; font-size: 0.8em; text-align: right; color: #888; }
        .col-action { width: 30px; text-align: center; }
        .x-btn { color: #ff0000; text-decoration: none; font-weight: bold; cursor: pointer; }
        .x-btn:hover { color: #fff; background: #ff0000; }
        .n-link { color: #00ff00; text-decoration: underline; font-weight: bold; }
        .n-link:hover { background: #00ff00; color: #000; }
    </style>
</head>
<body>
    <div class="nav-bar">
        <h1>SOURCE_STREAM({{ feed_id }})</h1>
        <div style="display:flex; gap:10px;">
            <form action="/refresh_feed/{{ feed_id }}" method="post" style="margin:0;">
                <button type="submit" class="sync-btn">[ SYNC_FEED ]</button>
            </form>
            <form action="/delete_feed/{{ feed_id }}" method="post" style="margin:0;">
                <button type="submit" class="del-btn">[ DELETE_SOURCE ]</button>
            </form>
        </div>
    </div>
    <a href="/" class="back-link">[ RETURN TO SOURCES ]</a>
    <hr>
    {% for article in articles %}
    <div class="article-row">
        <div class="col-title">
            <a href="/article/{{ article.id }}" class="{{ 'is-scraped' if article.content else '' }}">[ {{ article.title }} ]</a>
        </div>
        <div class="col-external">
            <a href="{{ article.link }}" target="_blank" class="n-link">N</a>
        </div>
        <div class="col-date">
            {{ article.pub_date }}
        </div>
        <div class="col-action">
            <a href="/delete_article/{{ article.id }}" class="x-btn" onclick="return confirm('Delete article?')">[X]</a>
        </div>
    </div>
    {% endfor %}
    {% if not articles %}
        <p>No articles found for this source.</p>
    {% endif %}
</body>
</html>
"""

MAN_PAGE_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <title>{{ article.title }}(1)</title>
    <style>
        body { font-family: "Courier New", Courier, monospace; background: #000000; color: #ffffff; padding: 50px; line-height: 1.4; max-width: 900px; margin: auto; }
        .man-header { text-align: left; font-weight: bold; margin-bottom: 20px; text-transform: uppercase; }
        .section-title { font-weight: bold; text-transform: uppercase; margin-top: 25px;  display: block; border-bottom: 1px solid #ccc; }
        .section-title2 { text-transform: uppercase; margin-top: 25px;  display: block; border-bottom: 1px solid #ccc; }
        .section-date { font-weight: bold; text-transform: uppercase; text-align: center; margin-top: 25px;  display: block; border-bottom: 1px solid #ccc; }
        .content { white-space: pre-wrap; margin-top: 10px; text-align: left; }
        .ref-link { color: #031; text-decoration: underline dotted #333; cursor: pointer; transition: color 0.2s; }
        .ref-link:hover { color: #00ff00 !important; background: #333; text-decoration: underline solid #00ff00; }
        .meta-box { background: #222; color: #ccc; padding: 10px; border: 1px solid #444; margin-top: 10px; font-size: 0.9em; }
        .footer { margin-top: 50px; border-top: 1px solid #fff; padding-top: 10px; }
        a { color: #fff; text-decoration: underline; }
        .refresh-btn { color: #00ff00; font-weight: bold; text-decoration: none; border: 1px solid #00ff00; padding: 2px 5px; font-size: 0.8em; }
        .refresh-btn:hover { background: #00ff00; color: #000; }
        .del-btn { color: #ff0000; font-weight: bold; text-decoration: none; border: 1px solid #ff0000; padding: 2px 5px; font-size: 0.8em; }
        .del-btn:hover { background: #ff0000; color: #fff; }
    </style>
</head>
<body>
    <span class="section-title2">{{ article.pub_date }}</span>
    <div class="man-header">{{ article.title | upper }}</div>
    <span class="section-title">SOURCE</span>
    <div class="content">{{ article.link }}</div>
    <span class="section-title">DESCRIPTION</span>
    <div class="content">{{ article.content | safe }}</div>
    <span class="section-title">SOURCE_METADATA</span>
    <div class="meta-box">
URL: {{ article.link }}<br>
DATE: {{ article.pub_date }}<br>
DB_ID: {{ article.id }}<br><br>
<a href="/refresh_article/{{ article.id }}" class="refresh-btn">[ RE-SCRAPE ]</a>
<a href="/delete_article/{{ article.id }}" class="del-btn">[ DELETE_ARTICLE ]</a>
    </div>
    <div class="footer">
        <a href="/">[ RETURN TO INDEX ]</a>
    </div>
</body>
</html>
"""

# --- ROUTES ---

@app.route('/')
def index():
    with get_db() as conn:
        feeds = conn.execute("SELECT * FROM feeds ORDER BY id ASC").fetchall()
    return render_template_string(INDEX_TEMPLATE, feeds=feeds)

@app.route('/feed/<int:feed_id>')
def view_feed(feed_id):
    with get_db() as conn:
        articles_raw = conn.execute('SELECT * FROM articles WHERE feed_id = ? ORDER BY id DESC LIMIT 50', (feed_id,)).fetchall()
    articles = []
    for a in articles_raw:
        d = dict(a)
        d['stripped_link'] = strip_protocol(a['link'])
        articles.append(d)
    return render_template_string(FEED_VIEW_TEMPLATE, articles=articles, feed_id=feed_id)

@app.route('/refresh_feed/<int:feed_id>', methods=['POST'])
def refresh_feed(feed_id):
    sync_feed(feed_id)
    return redirect(url_for('view_feed', feed_id=feed_id))

@app.route('/delete_article/<int:article_id>')
def delete_article(article_id):
    with get_db() as conn:
        conn.execute("DELETE FROM articles WHERE id = ?", (article_id,))
        conn.commit()
    return redirect(request.referrer or url_for('index'))

@app.route('/delete_feed/<int:feed_id>', methods=['POST'])
def delete_feed(feed_id):
    with get_db() as conn:
        cached_count = conn.execute("SELECT COUNT(*) as cnt FROM articles WHERE feed_id = ? AND content IS NOT NULL", (feed_id,)).fetchone()['cnt']
        if cached_count > 0:
            flash(f"Cannot delete source: {cached_count} cached articles still exist. Please delete them first.")
            return redirect(url_for('index'))
        conn.execute("DELETE FROM articles WHERE feed_id = ?", (feed_id,))
        conn.execute("DELETE FROM feeds WHERE id = ?", (feed_id,))
        conn.commit()
    return redirect(url_for('index'))

@app.route('/progress')
def progress():
    return jsonify(progress_tracker)

@app.route('/article/<int:article_id>')
def view_article(article_id):
    with get_db() as conn:
        row = conn.execute("SELECT * FROM articles WHERE id = ?", (article_id,)).fetchone()
    if not row: return "Article not found", 404
    article = dict(row)
    if not article['content']:
        content = scrape_article_content(article['link'])
        with get_db() as conn_update:
            conn_update.execute("UPDATE articles SET content = ? WHERE id = ?", (content, article_id))
            conn_update.commit()
        article['content'] = content
    return render_template_string(MAN_PAGE_TEMPLATE, article=article)

@app.route('/refresh_article/<int:article_id>')
def refresh_article(article_id):
    with get_db() as conn:
        row = conn.execute("SELECT link FROM articles WHERE id = ?", (article_id,)).fetchone()
    if row:
        content = scrape_article_content(row['link'])
        with get_db() as conn_update:
            conn_update.execute("UPDATE articles SET content = ? WHERE id = ?", (content, article_id))
            conn_update.commit()
    return redirect(url_for('view_article', article_id=article_id))

@app.route('/add', methods=['POST'])
def add():
    url = request.form.get('url')
    if url: add_feed(url)
    return redirect(url_for('index'))

@app.route('/refresh', methods=['POST'])
def refresh():
    thread = threading.Thread(target=background_update)
    thread.start()
    return redirect(url_for('index'))

if __name__ == '__main__':
    init_db()
    app.run(debug=True, port=5000)
