import os, httpx, json, html, markdown, asyncio
from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from starlette.middleware.sessions import SessionMiddleware  # starlette downloads along fastapi
from authlib.integrations.starlette_client import OAuth
from authlib.integrations.base_client.errors import MismatchingStateError
from google import genai
from collections import Counter
from google.genai import errors

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


gemini = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))
MODEL = "gemini-3-flash-preview"
MODELS = ["gemini-3.1-flash-lite", "gemini-3.5-flash-lite", "gemini-2.5-flash", MODEL]

DATA_NOTES = """
WHAT THE DATA IS:
- liked_video_titles: videos they pressed Like on (many are old, they may have forgotten them)
- top_channels_in_likes: (channel, how many of their likes came from it)
- subscriptions: channels they follow
- playlists: playlists they made, with some saved videos
- the number_of fields are exact counts
You do NOT have their watch history. Never say "watched" or claim how often
or how long they watched anything. Say "you liked", "you subscribed to",
"you saved". Only use numbers that appear in the data.
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
You are a brutally funny roast comedian. The person asked to be destroyed, so
be savage about their taste and choices.

You are given their data and a list of possible angles. Pick the 4 funniest
and write one paragraph for each.

Each paragraph has 2 or 3 sentences:
1. The evidence, with an exact title or number.
2. A punchline that ADDS something new: an exaggeration, a comparison, or an
   absurd conclusion. Test it: if the last sentence only repeats the evidence
   with an insult, rewrite it.

Example of the shape (invented data, do not copy):
"You liked a video called '10-Minute Abs' and a 3-hour mukbang. One of those
is a workout plan. The other one is your actual workout plan."

Rules:
- Never describe their habits. Every sentence is a joke or its setup.
- Talk directly to "you". Short sentences. Plain everyday words.
- Explain anything niche in a few words.
- Maximum 2 titles or names per paragraph.
- Roast their choices, never who they are. Do not guess about their job,
  school, money, social life, mood or relationships.
- No jokes about religion, health, disability, sexuality, appearance, family,
  race, nationality or politics. Never use phrases like "cry for help",
  "depressed", "anxious", or anything about self-harm.

Format (markdown):
## Brain Rot Score: NN/100
One savage sentence about the score.
## The Roast
Four short paragraphs.
## Verdict
One brutal sentence.

Under 250 words.
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
        for attempt in range(3):
            try:
                response = await gemini.aio.models.generate_content(
                    model=model, contents=prompt)
            except errors.ServerError as e:
                last_error = e
                await asyncio.sleep(2 ** attempt)   # waits 1, 2, then 4 seconds
                continue
            except errors.ClientError as e:
                if e.code in (404, 429):
                    print(f"Skipping {model}: {e.code}")   
                    last_error = e
                    break                                  # unavailable or out of quota: try the next model
                raise                                      # other client errors are real bugs
            
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

    while len(items) < max_items:
        r = await client.get(url, params=params, headers=headers)
        if r.status_code == 401:
            raise TokenExpired()  # google token expired
        if r.status_code != 200:
            raise Exception(r.text)   # shows YouTube's error in your terminal
        if r.status_code == 404 and "channelNOtFound" in r.text:
            return []       # no channel means nothing to fetch, not a failure

        data = r.json()
        items += data.get("items", [])

        next_page = data.get("nextPageToken")
        if not next_page:
            break                      # no more pages
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
        <h1>YouTube Likes Roast</h1>
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
    return await oauth.google.authorize_redirect(request, redirect_uri) # attach state to request and after complition of login , google send the user back to redirect_url

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
        angles = await ask_gemini(ANGLE_FINDER + DATA_NOTES + "\n\nDATA:\n" + data_text)
        roast_text = await ask_gemini(
            ROAST_WRITER + DATA_NOTES + "\n\nDATA:\n" + data_text + "\n\nANGLES FOUND:\n" + angles)
    except errors.ServerError:
        return HTMLResponse(PAGE.replace("__BODY__",
            "<h2>The AI is overloaded right now</h2>"
            "<p>This usually clears up in a minute. Try again.</p>"
            "<a class='btn' href='/'>Back</a>"))
        
    body = markdown.markdown(html.escape(roast_text))
    return HTMLResponse(PAGE.replace("__BODY__", body))