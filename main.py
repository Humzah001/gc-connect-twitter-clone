from datetime import datetime
from urllib.parse import parse_qsl, unquote, urlencode
import google.oauth2.id_token
import re
import starlette.status as status
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from google.auth.transport import requests as google_requests
from pymongo.mongo_client import MongoClient
from pymongo.server_api import ServerApi


APP_TITLE = "Twitter Replica"
APP_HOME = "/home"
MONGO_DB = "A2-3181143"
MAX_TWEET_LEN = 280

uri = "MONGO_URI_REMOVED"

client = MongoClient(uri, server_api=ServerApi("1"))

try:
    client.admin.command("ping")
    print("Connected to MongoDB")
except Exception as e:
    print(e)


app = FastAPI(
    title=APP_TITLE,
    description="CP Assignment 2",
)


@app.middleware("http")
async def stop_html_cache(request: Request, call_next):
    response = await call_next(request)

    content_type = response.headers.get("content-type", "")
    if content_type.partition(";")[0].strip().lower() == "text/html":
        response.headers["Cache-Control"] = "private, no-store, max-age=0"

    return response


app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

db = client[MONGO_DB]
user_collection = db["users"]
tweet_collection = db["tweets"]

firebase_request_adapter = google_requests.Request()

FLASH_ERRORS = {
    "username_empty": "Please enter a username.",
    "username_taken": "That username is already taken. Try another.",
    "tweet_empty": "Your tweet cannot be empty.",
    "tweet_long": "Tweets must be 280 characters or fewer.",
}


def add_query(url, **params):
    if "?" in url:
        base, query = url.split("?", 1)
        values = dict(parse_qsl(query))
    else:
        base = url
        values = {}

    for key, value in params.items():
        if value is None:
            values.pop(key, None)
        else:
            values[key] = str(value)

    query_string = urlencode(values)

    if query_string:
        return base + "?" + query_string

    return base


def get_flash_message(request: Request):
    err = request.query_params.get("err", "").strip()

    if err:
        message = FLASH_ERRORS.get(err, "Something went wrong. Please try again.")
        return message, "error"

    return None, None


def check_firebase_token(id_token):
    if not id_token:
        return None

    if "%" in id_token:
        id_token = unquote(id_token)

    try:
        user_token = google.oauth2.id_token.verify_firebase_token(
            id_token,
            firebase_request_adapter,
            clock_skew_in_seconds=60,
        )
        return user_token
    except ValueError as e:
        print(e)
        return None


def get_user(user_token):
    user_id = user_token["user_id"]

    user = user_collection.find_one({"user_id": user_id})

    if user is None:
        new_user = {
            "user_id": user_id,
            "email": user_token.get("email", ""),
            "username": None,
        }

        user_collection.insert_one(new_user)
        user = user_collection.find_one({"user_id": user_id})

    return user


def username_exists(username):
    username = username.strip()

    if not username:
        return False

    pattern = re.compile("^" + re.escape(username) + "$", re.IGNORECASE)
    user = user_collection.find_one({"username": pattern})

    return user is not None


def has_username(user):
    if user is None:
        return False

    username = user.get("username")

    if username is None:
        return False

    return len(str(username).strip()) > 0


def sort_tweets(tweets):
    n = len(tweets)

    for i in range(n):
        for j in range(i + 1, n):
            first_date = tweets[i].get("created_at", datetime.min)
            second_date = tweets[j].get("created_at", datetime.min)

            if first_date < second_date:
                temp = tweets[i]
                tweets[i] = tweets[j]
                tweets[j] = temp

    return tweets


def get_my_tweets(user_id):
    tweets = []

    for tweet in tweet_collection.find({"user_id": user_id}):
        tweets.append(tweet)

    tweets = sort_tweets(tweets)

    return tweets


@app.get("/", response_class=HTMLResponse)
async def root(request: Request):
    id_token = request.cookies.get("token")
    user_token = check_firebase_token(id_token)

    if user_token:
        query = request.url.query
        redirect_url = APP_HOME

        if query:
            redirect_url = redirect_url + "?" + str(query)

        return RedirectResponse(
            redirect_url,
            status_code=status.HTTP_302_FOUND,
        )

    flash_message, flash_kind = get_flash_message(request)

    return templates.TemplateResponse(
        "login.html",
        {
            "request": request,
            "app_title": APP_TITLE,
            "flash_message": flash_message,
            "flash_kind": flash_kind,
        },
    )


@app.get("/home", response_class=HTMLResponse)
async def home(request: Request):
    id_token = request.cookies.get("token")
    user_token = check_firebase_token(id_token)

    if not user_token:
        return RedirectResponse("/", status_code=status.HTTP_302_FOUND)

    user = get_user(user_token)

    if not has_username(user):
        return RedirectResponse(
            "/set-username",
            status_code=status.HTTP_302_FOUND,
        )

    flash_message, flash_kind = get_flash_message(request)
    tweets = get_my_tweets(user_token["user_id"])

    return templates.TemplateResponse(
        "home.html",
        {
            "request": request,
            "app_title": APP_TITLE,
            "user_token": user_token,
            "user_info": user,
            "flash_message": flash_message,
            "flash_kind": flash_kind,
            "my_tweets": tweets,
            "max_tweet_len": MAX_TWEET_LEN,
        },
    )


@app.get("/set-username", response_class=HTMLResponse)
async def set_username_page(request: Request):
    id_token = request.cookies.get("token")
    user_token = check_firebase_token(id_token)

    if not user_token:
        return RedirectResponse("/", status_code=status.HTTP_302_FOUND)

    user = get_user(user_token)

    if has_username(user):
        return RedirectResponse(APP_HOME, status_code=status.HTTP_302_FOUND)

    flash_message, flash_kind = get_flash_message(request)

    return templates.TemplateResponse(
        "set-username.html",
        {
            "request": request,
            "app_title": APP_TITLE,
            "user_token": user_token,
            "user_info": user,
            "flash_message": flash_message,
            "flash_kind": flash_kind,
        },
    )


@app.post("/set-username", response_class=RedirectResponse)
async def save_username(request: Request):
    id_token = request.cookies.get("token")
    user_token = check_firebase_token(id_token)

    if not user_token:
        return RedirectResponse("/", status_code=status.HTTP_302_FOUND)

    user = get_user(user_token)

    if has_username(user):
        return RedirectResponse(APP_HOME, status_code=status.HTTP_302_FOUND)

    form = await request.form()
    username = form.get("username", "")
    username = str(username).strip()

    if not username:
        return RedirectResponse(
            add_query("/set-username", err="username_empty"),
            status_code=status.HTTP_302_FOUND,
        )

    if username_exists(username):
        return RedirectResponse(
            add_query("/set-username", err="username_taken"),
            status_code=status.HTTP_302_FOUND,
        )

    user_collection.update_one(
        {"user_id": user_token["user_id"]},
        {"$set": {"username": username}},
    )

    return RedirectResponse(APP_HOME, status_code=status.HTTP_302_FOUND)


@app.post("/add-tweet", response_class=RedirectResponse)
async def add_tweet(request: Request):
    id_token = request.cookies.get("token")
    user_token = check_firebase_token(id_token)

    if not user_token:
        return RedirectResponse("/", status_code=status.HTTP_302_FOUND)

    user = get_user(user_token)

    if not has_username(user):
        return RedirectResponse(
            "/set-username",
            status_code=status.HTTP_302_FOUND,
        )

    form = await request.form()
    text = form.get("text", "")
    text = str(text).strip()

    if not text:
        return RedirectResponse(
            add_query(APP_HOME, err="tweet_empty"),
            status_code=status.HTTP_302_FOUND,
        )

    if len(text) > MAX_TWEET_LEN:
        return RedirectResponse(
            add_query(APP_HOME, err="tweet_long"),
            status_code=status.HTTP_302_FOUND,
        )

    tweet = {
        "user_id": user_token["user_id"],
        "text": text,
        "created_at": datetime.utcnow(),
    }

    tweet_collection.insert_one(tweet)

    return RedirectResponse(APP_HOME, status_code=status.HTTP_302_FOUND)