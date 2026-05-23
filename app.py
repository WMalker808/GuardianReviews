import os
import re
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed
from flask import Flask, render_template, request, jsonify
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)

CAPI_BASE = "https://content.guardianapis.com/search"
API_KEY = os.environ["GUARDIAN_API_KEY"]

MUSIC_SECTIONS = {"rock-pop", "classical", "jazz", "folk", "electronic", "hiphop", "country", "world"}

SECTIONS = {
    "film": {"section": "film", "tag": "tone/reviews"},
    "tv": {"section": "tv-and-radio", "tag": "tone/reviews"},
    "rock-pop": {"section": "music", "tag": "music/popandrock,tone/albumreview"},
    "classical": {"section": "music", "tag": "music/classical,tone/albumreview"},
    "jazz": {"section": "music", "tag": "music/jazz,tone/albumreview"},
    "folk": {"section": "music", "tag": "music/folk,tone/albumreview"},
    "electronic": {"section": "music", "tag": "music/electronicmusic,tone/albumreview"},
    "hiphop": {"section": "music", "tag": "music/hip-hop,tone/albumreview"},
    "country": {"section": "music", "tag": "music/country,tone/albumreview"},
    "world": {"section": "music", "tag": "music/worldmusic,tone/albumreview"},
    "theatre": {"section": "stage", "tag": "tone/reviews"},
    "dance": {"section": "stage", "tag": "dance,tone/reviews"},
    "opera": {"section": "music", "tag": "music/opera,tone/reviews"},
    "comedy": {"section": "stage", "tag": "stage/comedy,tone/reviews"},
    "books": {"section": "books", "tag": "tone/reviews"},
    "games": {"section": "games", "tag": "tone/reviews"},
    "art": {"section": "artanddesign", "tag": "tone/reviews"},
    "restaurants": {"section": "lifeandstyle", "tag": "food/food,tone/reviews"},
}

RELEASE_KEYWORDS = [
    "in cinemas", "in cinema", "on general release", "available on", "streaming on",
    "on netflix", "on amazon", "on prime", "on disney", "on apple tv", "on bbc",
    "on itv", "on channel 4", "on channel 5", "on sky", "on now", "on mubi",
    "on britbox", "on itvx", "on all 4", "on demand", "on paramount",
    "released on", "out now", "opens", "premieres",
]


def extract_release_info(body_html):
    if not body_html:
        return None
    paragraphs = re.findall(r'<p[^>]*>(.*?)</p>', body_html, re.DOTALL | re.IGNORECASE)
    for raw in reversed(paragraphs[-4:]):
        text = re.sub(r'<[^>]+>', '', raw).strip()
        if text and any(kw in text.lower() for kw in RELEASE_KEYWORDS):
            return text
    return None


def fetch_reviews(section_key, star_rating=None, page=1, music_format=None, from_date=None, to_date=None):
    section_config = dict(SECTIONS.get(section_key, SECTIONS["film"]))
    show_body = section_key in ("film", "tv")

    if section_key in MUSIC_SECTIONS and music_format == "live":
        section_config["tag"] = section_config["tag"].replace("tone/albumreview", "tone/livereview")

    fields = "headline,byline,thumbnail,starRating,trailText"
    if show_body:
        fields += ",body"

    params = {
        "show-fields": fields,
        "page": page,
        "page-size": 20,
        "order-by": "newest",
        "api-key": API_KEY,
    }
    params.update(section_config)

    if star_rating:
        params["star-rating"] = star_rating
    if from_date:
        params["from-date"] = from_date
    if to_date:
        params["to-date"] = to_date

    resp = requests.get(CAPI_BASE, params=params, timeout=10)
    resp.raise_for_status()
    return resp.json().get("response", {})


def merge_responses(responses):
    all_results = []
    total = 0
    max_pages = 1
    current_page = 1

    for r in responses:
        all_results.extend(r.get("results", []))
        total += r.get("total", 0)
        max_pages = max(max_pages, r.get("pages", 1))
        current_page = r.get("currentPage", 1)

    all_results.sort(key=lambda x: x.get("webPublicationDate", ""), reverse=True)

    return {
        "results": all_results,
        "total": total,
        "pages": max_pages,
        "currentPage": current_page,
    }


def format_results(data, section_key):
    results = []
    for item in data.get("results", []):
        fields = item.get("fields", {})
        release_info = None
        if section_key in ("film", "tv"):
            release_info = extract_release_info(fields.get("body", ""))

        results.append({
            "id": item.get("id"),
            "webUrl": item.get("webUrl"),
            "webPublicationDate": item.get("webPublicationDate"),
            "headline": fields.get("headline", item.get("webTitle")),
            "byline": fields.get("byline", ""),
            "trailText": fields.get("trailText", ""),
            "thumbnail": fields.get("thumbnail", ""),
            "starRating": fields.get("starRating"),
            "releaseInfo": release_info,
        })
    return results


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/analytics")
def api_analytics():
    from_date = request.args.get("from_date", "")
    to_date = request.args.get("to_date", "")

    def fetch_count(section_key, star):
        section_config = dict(SECTIONS[section_key])
        params = {
            "page-size": 1,
            "page": 1,
            "star-rating": str(star),
            "api-key": API_KEY,
        }
        if from_date:
            params["from-date"] = from_date
        if to_date:
            params["to-date"] = to_date
        params.update(section_config)
        resp = requests.get(CAPI_BASE, params=params, timeout=15)
        resp.raise_for_status()
        return resp.json().get("response", {}).get("total", 0)

    results = {key: {} for key in SECTIONS}
    tasks = [(section, star) for section in SECTIONS for star in range(1, 6)]

    try:
        with ThreadPoolExecutor(max_workers=10) as executor:
            futures = {executor.submit(fetch_count, s, r): (s, r) for s, r in tasks}
            for future in as_completed(futures):
                section, star = futures[future]
                try:
                    results[section][str(star)] = future.result()
                except Exception:
                    results[section][str(star)] = 0
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    return jsonify(results)


@app.route("/api/reviews")
def api_reviews():
    section = request.args.get("section", "film")
    stars_param = request.args.get("stars", "")
    music_format = request.args.get("format", "albums")
    page = int(request.args.get("page", 1))
    from_date = request.args.get("from_date", "")
    to_date = request.args.get("to_date", "")

    if section not in SECTIONS:
        return jsonify({"error": "Invalid section"}), 400

    star_ratings = [s.strip() for s in stars_param.split(",") if s.strip()]

    try:
        if len(star_ratings) <= 1:
            data = fetch_reviews(section, star_ratings[0] if star_ratings else None, page, music_format, from_date, to_date)
        else:
            with ThreadPoolExecutor() as executor:
                futures = {executor.submit(fetch_reviews, section, sr, page, music_format, from_date, to_date): sr for sr in star_ratings}
                responses = [f.result() for f in as_completed(futures)]
            data = merge_responses(responses)
    except requests.HTTPError as e:
        return jsonify({"error": str(e)}), 502
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    return jsonify({
        "results": format_results(data, section),
        "currentPage": data.get("currentPage", 1),
        "pages": data.get("pages", 1),
        "total": data.get("total", 0),
    })


if __name__ == "__main__":
    app.run(debug=True, port=8080)
