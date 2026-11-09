import os, httpx, json, html, markdown, asyncio
from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from starlette.middleware.sessions import SessionMiddleware  # starlette downloads along fastapi
from authlib.integrations.starlette_client import OAuth
from authlib.integrations.base_client.errors import MismatchingStateError
from google import genai
from collections import Counter
from google.genai import errors, types

load_dotenv()  # reads your .env file
BASE_URL = os.getenv("BASE_URL", "http://localhost:8000")

app = FastAPI()
app.add_middleware(SessionMiddleware, secret_key=os.getenv("SESSION_SECRET"), https_only=BASE_URL.startswith("https"))  # recieve and attach the cookies

oauth = OAuth()
oauth.register(
    name="google",
    client_id=os.getenv("GOOGLE_CLIENT_ID"),
    client_secret=os.getenv("GOOGLE_CLIENT_SECRET"),
    server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",  # gives all google urls
    client_kwargs={
        "scope": "openid email https://www.googleapis.com/auth/youtube.readonly"    # user identity, email, and youtube data
    },
)


gemini = genai.Client(api_key=os.getenv("GEMINI_API_KEY"),http_options=types.HttpOptions(timeout=30000),)

MODELS = []
for model in gemini.models.list():
    if "generateContent" in (model.supported_actions or []) and "flash" in model.name and "image" not in model.name and "tts" not in model.name:
        MODELS.append(model.name)
    
DATA_NOTES = """
WHAT THE DATA IS:
You only have likes, subscriptions and playlists, not watch history.
Never say "watched". Only use numbers that appear in the data.
"""

ANGLE_FINDER = """
You are a comedy writer's research assistant. Read the YouTube data and find
the 8 funniest ANGLES about this person. An angle is something specific that
you could only say because of THIS data. Look for:
- an absurd mismatch between ambition and effort (a 57-second tutorial for a huge skill)
- two things in the data that clash
- a funny number or ratio
- one odd item that stands out from everything else
- something suspiciously repeated
For each angle, give the exact evidence (titles, channel names, numbers) and
one line on why it is funny. Do NOT write the jokes yet. Skip any angle that
is only a description of a habit.
"""

ROAST_WRITER = """
You are a savage but funny YouTube roaster.
Roast this person based on their YouTube data.
Be witty, sarcastic, and a bit mean, but keep it light-hearted.
Don't just list their videos. Make fun of patterns, weird obsessions, and contradictions.
Start with a "Brain Rot Score" out of 100.
End with a "Verdict": one short sentence that sums up their whole YouTube
personality in a way anyone would understand. It should be a simple comparison
or exaggeration, not a random phrase.
Example of the shape: Your YouTube is a gym membership, a library card and
a 3am doom-scroll in one account.
Only roast things that are actually in the data. Never invent numbers or facts.
If a joke needs explaining, don't use it.
Use simple, everyday words. Under 200 words.
"""

PAGE = """
<html>
<head>
  <meta charset="utf-8">
  <title>Your YouTube Roast</title>
  <style>
    body { font-family: sans-serif; max-width: 650px; margin: 40px auto;
           padding: 0 16px; line-height: 1.6; font-size: 18px; }
    h2 { margin-top: 32px; }
    .btn { display: inline-block; background: #e11d48; color: white;
           padding: 14px 28px; border-radius: 8px; text-decoration: none;
           font-size: 18px; }
    .busy { opacity: 0.6; pointer-events: none; }
    .small { font-size: 14px; color: #666; }
  </style>
</head>
<body>__BODY__</body>
</html>
"""
class TokenExpired(Exception):
    pass

async def ask_gemini(prompt):
    last_error = None
    for model in MODELS:
        for attempt in range(2):
            try:
                response = await gemini.aio.models.generate_content(  # uses .aio with await so that server wont stop, aio is a async
                    model=model, contents=prompt)
            except errors.ServerError as e:  # google server's problem, we should try again
                last_error = e
                print(f"{model}: server error {e.code}, retry {attempt + 1}/3")
                await asyncio.sleep(2 ** attempt)   # waits 1, 2, then 4 seconds
                continue
            except errors.ClientError as e:  # request has a problem , we shouldn't try again
                if e.code in (404, 429):  # unavailable or out of quota: try the next model
                    print(f"Skipping {model}: {e.code}\n\n")   
                    last_error = e
                    break                                  
                raise        # raise the current expection, same as writing raise e
            
            if response.text:
                print("Answered by", model)
                return response.text

            reason = response.candidates[0].finish_reason if response.candidates else None
            print(f"{model} gave an empty reply. finish_reason={reason}, feedback={response.prompt_feedback}")
            last_error = Exception("empty reply")
            break
        
    raise last_error

def prepare_for_roast(data):
    liked = [v for v in data["liked"]
             if v["title"] not in ("Deleted video", "Private video")]
    counts = Counter(v["channel"] for v in liked)
    return {
        "number_of_liked_videos": len(liked),
        "number_of_subscriptions": len(data["subscriptions"]),
        "top_channels_in_likes": counts.most_common(8),
        "liked_video_titles": [v["title"] for v in liked[:60]],
        "subscriptions": data["subscriptions"][:60],
        "playlists": {name: [v["title"] for v in vids[:8]]
                      for name, vids in data["playlists"].items()},
    }

async def fetch_all(client, url, params, access_token, max_items):
    items = []
    headers = {"Authorization": f"Bearer {access_token}"}

    seen_tokens = set()
    max_pages = (max_items + 49) // 50 # ceiling division, means if items are 201 then we make 5 calls instead of 4
    for page in range(max_pages):
        r = await client.get(url, params=params, headers=headers)
        if r.status_code == 401:
            raise TokenExpired()  # google token expired
        if r.status_code == 404 and "channelNotFound" in r.text:
            return []       # no channel means nothing to fetch, not a failure
        if r.status_code != 200:   # 200 means request was successful
            raise Exception(f"From fetch all:\n\n{r.text}\n\n")   # shows YouTube's error in your terminal

        data = r.json()
        items += data.get("items", [])

        next_page = data.get("nextPageToken")
        if next_page in seen_tokens:
            break                      # no more pages
        
        seen_tokens.add(next_page)
        params["pageToken"] = next_page

    return items[:max_items]

async def collect_data(access_token):
    base = "https://www.googleapis.com/youtube/v3"

    async with httpx.AsyncClient() as client:                           # create a client to make request to youtuve
        liked = await fetch_all(client, f"{base}/videos",                # snippet: basic info (title, channel name, description, upload date, thumbnails)
            {"part": "snippet", "myRating": "like", "maxResults": 50},     # liked videos, myRating means the vidoes i liked
            access_token, 200)
        subs = await fetch_all(client, f"{base}/subscriptions",           # subscribed channels, mine means my subcriptions
            {"part": "snippet", "mine": "true", "maxResults": 50},         
            access_token, 200)
        playlists = await fetch_all(client, f"{base}/playlists",          # playlist names only
            {"part": "snippet", "mine": "true", "maxResults": 50},
            access_token, 50)
        
        playlist_videos = {}          # each playlist's vidoes's info
        for p in playlists[:10]:      # first 10 playlists, to keep requests low
            items = await fetch_all(client, f"{base}/playlistItems",
                {"part": "snippet", "playlistId": p["id"], "maxResults": 50},
                access_token, 50)
            playlist_videos[p["snippet"]["title"]] = [
                {"title": i["snippet"]["title"],
                "channel": i["snippet"].get("videoOwnerChannelTitle", "")}
                for i in items
            ]

    return { 
        "liked": [
            {"title": v["snippet"]["title"],
             "channel": v["snippet"]["channelTitle"],
             "category": v["snippet"]["categoryId"]}
            for v in liked
        ],
        "subscriptions": [s["snippet"]["title"] for s in subs],
        "playlists": playlist_videos,
    }


@app.get("/", response_class=HTMLResponse) # it tell fastapi that the response is in html and not json, we have to tell fastapi cuz we are returning the data as html
def home(request: Request):
    user = request.session.get("user")
    if user:
        email = html.escape(user["email"])
        body = f"""
        <h1>YouTube Roast</h1>
        <p>Logged in as {email}</p>
        <a class="btn" href="/roast"
           onclick="this.textContent='Roasting you... up to 40 seconds'; this.classList.add('busy');">
           Roast me</a>
        <p><a href="/logout">Log out</a></p>
        """
    else:
        body = """
        <h1>YouTube Roast</h1>
        <p>Log in with Google and get roasted based on your liked videos,
        subscriptions and playlists.</p>
        <a class="btn" href="/login">Login with Google</a>
        <p>On Google's screen, tick the box that lets this app view your YouTube account, then press Continue.</p>
        <p class="small">This app only reads your YouTube data (it can't change anything),
        and it doesn't save any of it.</p>
        """
    return PAGE.replace("__BODY__", body)

@app.get("/login")
async def login(request: Request):
    redirect_uri = f"{BASE_URL}/callback"
    return await oauth.google.authorize_redirect(request, redirect_uri, prompt="select_account") # attach state to request and after complition of login , google send the user back to redirect_url

@app.get("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/")

@app.get("/callback")
async def callback(request: Request):
    try:
        token = await oauth.google.authorize_access_token(request)
    except MismatchingStateError :
        return RedirectResponse("/")
        
    if "youtube.readonly" not in token.get("scope", ""):
        request.session.clear()
        return HTMLResponse(PAGE.replace("__BODY__",
            "<h2>One more step</h2>"
            "<p>On Google's screen, tick the box that lets this app view your YouTube account, then press Continue.</p>"
            "<a class='btn' href='/login'>Try again</a>"))
        
    request.session["user"] = {"email": token["userinfo"]["email"]}
    request.session["token"] = token["access_token"]
    return RedirectResponse("/")

@app.get("/roast")
async def data(request: Request):
    access_token = request.session.get("token")
    if not access_token:
        return RedirectResponse("/")
    
    try:
        data = await collect_data(access_token)
    except TokenExpired:
        request.session.clear()
        return RedirectResponse("/login")
    
    roast_data = prepare_for_roast(data)
    data_text = json.dumps(roast_data, ensure_ascii=False)

    try:
        # angles = await ask_gemini(ANGLE_FINDER + DATA_NOTES + "\n\nDATA:\n" + data_text)
        roast_text = await ask_gemini(
            ROAST_WRITER + DATA_NOTES + "\n\nDATA:\n" + data_text ) # + "\n\nANGLES FOUND:\n" + angles
    except errors.APIError:    # quota errors or gemini traffic errors, quota means limit of APIs 
        return HTMLResponse(PAGE.replace("__BODY__",
            "<h2>The AI is overloaded right now</h2>"
            "<p>This usually clears up in a minute. Try again.</p>"
            "<a class='btn' href='/'>Back</a>"))
        
    body = markdown.markdown(html.escape(roast_text))
    body += '<p><a class="btn" href="/">Back</a> &nbsp <a class="btn" href="/logout">Log out</a></p>'
    return HTMLResponse(PAGE.replace("__BODY__", body))