from datetime import datetime, timedelta
from urllib.parse import parse_qsl, quote, unquote, urlencode
import google.oauth2.id_token
import re
import starlette.status as status
from azure.storage.blob import (AccessPolicy, BlobServiceClient, ContainerSasPermissions, ContentSettings, PublicAccess,)
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from google.auth.transport import requests as google_requests
from pymongo.mongo_client import MongoClient
from pymongo.server_api import ServerApi


APP_TITLE = "Twitter"
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
    description="CP Assignment-2",
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
follow_collection = db["follows"]

AZURE_CONNECTION_STRING = (
    "DefaultEndpointsProtocol=http;"
    "AccountName=devstoreaccount1;"
    "AccountKey=Eby8vdM02xNOcqFlqUwJPLlmEtlCDXJ1OUzFT50uSRZ6IFsuFq2UVErCz4I6tq/K1SZFPTOtr/KBHBeksoGMGw==;"
    "BlobEndpoint=http://127.0.0.1:10000/devstoreaccount1;"
)

AZURE_CONTAINER_NAME = "3181143-images"

azure_service_client = BlobServiceClient.from_connection_string(
    AZURE_CONNECTION_STRING
)
azure_container_client = azure_service_client.get_container_client(
    AZURE_CONTAINER_NAME
)

if not azure_container_client.exists():
    azure_container_client.create_container()

access_policy = AccessPolicy(
    permission=ContainerSasPermissions(read=True),
    expiry=datetime.now() + timedelta(hours=24),
    start=datetime.now() - timedelta(minutes=1),
)
identifiers = {"read": access_policy}
azure_container_client.set_container_access_policy(
    signed_identifiers=identifiers,
    public_access=PublicAccess.CONTAINER,
)

firebase_request_adapter = google_requests.Request()

FLASH_ERRORS = {
    "username_empty": "Please enter a username.",
    "username_taken": "That username is already taken. Try another.",
    "username_invalid": "Username may only use letters, numbers, and underscores (max 64).",
    "tweet_empty": "Your tweet cannot be empty.",
    "tweet_long": "Tweets must be 280 characters or fewer.",
    "follow_self": "You cannot follow yourself.",
    "profile_not_found": "That user was not found.",
    "photo_missing": "Please choose an image file.",
    "photo_bad_type": "Profile pictures must be JPG or PNG.",
    "photo_upload_failed": "Upload failed. Check Azurite/Azure blob settings and credentials.",
}

FLASH_OK = {
    "photo_updated": "Profile picture updated.",
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

    ok = request.query_params.get("ok", "").strip()
    if ok:
        ok_message = FLASH_OK.get(ok)
        if ok_message:
            return ok_message, "success"

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
            "profile_image_url": None,
        }

        user_collection.insert_one(new_user)
        user = user_collection.find_one({"user_id": user_id})

    return user


def ensure_profile_image_field(user):
    if user is None:
        return user
    if "profile_image_url" not in user:
        user_collection.update_one(
            {"user_id": user["user_id"]},
            {"$set": {"profile_image_url": None}},
        )
        user = user_collection.find_one({"user_id": user["user_id"]})
    return user


def username_exists(username):
    username = username.strip()

    if not username:
        return False

    pattern = re.compile("^" + re.escape(username) + "$", re.IGNORECASE)
    user = user_collection.find_one({"username": pattern})

    return user is not None


def get_user_by_username(username):
    username = str(username).strip()
    if not username:
        return None
    pattern = re.compile("^" + re.escape(username) + "$", re.IGNORECASE)
    return user_collection.find_one({"username": pattern})


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


def search_users_by_prefix(prefix):
    prefix = prefix.strip().lower()
    results = []
    if prefix == "":
        return results

    for u in user_collection.find({}):
        name = u.get("username")
        if name is None:
            continue
        n = str(name).strip()
        if len(n) == 0:
            continue
        lower = n.lower()
        if lower.startswith(prefix):
            results.append(u)

    return results


def search_tweets_by_prefix(prefix):
    prefix = prefix.strip().lower()
    results = []
    if prefix == "":
        return results

    for tweet in tweet_collection.find({}):
        text = tweet.get("text", "")
        t = str(text).strip()
        if len(t) == 0:
            continue
        if t.lower().startswith(prefix):
            author = user_collection.find_one({"user_id": tweet.get("user_id")})
            handle = "unknown"
            if author is not None and author.get("username"):
                handle = str(author.get("username"))
            row = {"tweet": tweet, "author_username": handle}
            results.append(row)

    return results


def is_following(follower_id, following_id):
    row = follow_collection.find_one(
        {"follower_id": follower_id, "following_id": following_id}
    )
    return row is not None


def profile_picture_allowed(filename, content_type):
    if filename is None:
        filename = ""
    fn = str(filename).lower()
    ext_ok = False
    if fn.endswith(".jpg"):
        ext_ok = True
    if fn.endswith(".jpeg"):
        ext_ok = True
    if fn.endswith(".png"):
        ext_ok = True
    if not ext_ok:
        return False, ""

    ct = ""
    if content_type is not None:
        ct = str(content_type).lower()

    if ct == "image/jpeg" or ct == "image/png":
        return True, ct

    if fn.endswith(".png"):
        return True, "image/png"

    return True, "image/jpeg"


def upload_profile_bytes(user_id, filename, file_bytes, content_type):
    fn_lower = str(filename).lower()

    ok, ctype = profile_picture_allowed(filename, content_type)
    if not ok:
        return None

    upload_type = ctype
    if upload_type != "image/jpeg" and upload_type != "image/png":
        if fn_lower.endswith(".png"):
            upload_type = "image/png"
        else:
            upload_type = "image/jpeg"

    blob_name = "profile-pics/" + user_id + "/profile"

    blob_client = azure_container_client.get_blob_client(blob_name)
    blob_client.upload_blob(
        file_bytes,
        overwrite=True,
        content_settings=ContentSettings(content_type=upload_type),
    )

    return blob_client.url


def viewer_from_request(request):
    id_token = request.cookies.get("token")
    user_token = check_firebase_token(id_token)
    if not user_token:
        return None, None
    user = get_user(user_token)
    user = ensure_profile_image_field(user)
    if not has_username(user):
        return user_token, None
    return user_token, user


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
    user = ensure_profile_image_field(user)

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


@app.get("/search", response_class=HTMLResponse)
async def search_page(request: Request):
    user_token, viewer = viewer_from_request(request)
    if user_token is not None and viewer is None:
        return RedirectResponse("/set-username", status_code=status.HTTP_302_FOUND)

    q_user = request.query_params.get("q_user", "").strip()
    q_tweet = request.query_params.get("q_tweet", "").strip()

    user_hits = search_users_by_prefix(q_user)
    tweet_hits = search_tweets_by_prefix(q_tweet)

    flash_message, flash_kind = get_flash_message(request)

    return templates.TemplateResponse(
        "search.html",
        {
            "request": request,
            "app_title": APP_TITLE,
            "user_token": user_token,
            "user_info": viewer,
            "q_user": q_user,
            "q_tweet": q_tweet,
            "user_hits": user_hits,
            "tweet_hits": tweet_hits,
            "flash_message": flash_message,
            "flash_kind": flash_kind,
        },
    )


@app.get("/profile/{username}", response_class=HTMLResponse)
async def profile_page(request: Request, username: str):
    user_token, viewer = viewer_from_request(request)
    if user_token is not None and viewer is None:
        return RedirectResponse("/set-username", status_code=status.HTTP_302_FOUND)

    flash_message, flash_kind = get_flash_message(request)

    profile_user = get_user_by_username(username)
    if profile_user is None:
        return templates.TemplateResponse(
            "profile.html",
            {
                "request": request,
                "app_title": APP_TITLE,
                "user_token": user_token,
                "user_info": viewer,
                "profile_user": None,
                "profile_tweets": [],
                "is_own_profile": False,
                "is_following": False,
                "flash_message": flash_message,
                "flash_kind": flash_kind,
            },
        )

    profile_user = ensure_profile_image_field(profile_user)

    all_tweets = get_my_tweets(profile_user["user_id"])
    profile_tweets = []
    i = 0
    while i < len(all_tweets) and i < 10:
        profile_tweets.append(all_tweets[i])
        i = i + 1

    is_own_profile = False
    following_flag = False

    if user_token is not None and viewer is not None:
        if viewer["user_id"] == profile_user["user_id"]:
            is_own_profile = True
        else:
            following_flag = is_following(
                user_token["user_id"], profile_user["user_id"]
            )

    return templates.TemplateResponse(
        "profile.html",
        {
            "request": request,
            "app_title": APP_TITLE,
            "user_token": user_token,
            "user_info": viewer,
            "profile_user": profile_user,
            "profile_tweets": profile_tweets,
            "is_own_profile": is_own_profile,
            "is_following": following_flag,
            "flash_message": flash_message,
            "flash_kind": flash_kind,
        },
    )


@app.post("/follow", response_class=RedirectResponse)
async def follow_user_route(request: Request):
    id_token = request.cookies.get("token")
    user_token = check_firebase_token(id_token)
    if not user_token:
        return RedirectResponse("/", status_code=status.HTTP_302_FOUND)

    viewer = get_user(user_token)
    if not has_username(viewer):
        return RedirectResponse("/set-username", status_code=status.HTTP_302_FOUND)

    form = await request.form()
    target_name = str(form.get("username", "")).strip()
    profile_user = get_user_by_username(target_name)

    safe_path = "/profile/" + quote(target_name, safe="")

    if profile_user is None:
        return RedirectResponse(
            add_query(APP_HOME, err="profile_not_found"),
            status_code=status.HTTP_302_FOUND,
        )

    if profile_user["user_id"] == user_token["user_id"]:
        return RedirectResponse(
            add_query(safe_path, err="follow_self"),
            status_code=status.HTTP_302_FOUND,
        )

    if not is_following(user_token["user_id"], profile_user["user_id"]):
        follow_collection.insert_one(
            {
                "follower_id": user_token["user_id"],
                "following_id": profile_user["user_id"],
            }
        )

    return RedirectResponse(safe_path, status_code=status.HTTP_302_FOUND)


@app.post("/unfollow", response_class=RedirectResponse)
async def unfollow_user_route(request: Request):
    id_token = request.cookies.get("token")
    user_token = check_firebase_token(id_token)
    if not user_token:
        return RedirectResponse("/", status_code=status.HTTP_302_FOUND)

    viewer = get_user(user_token)
    if not has_username(viewer):
        return RedirectResponse("/set-username", status_code=status.HTTP_302_FOUND)

    form = await request.form()
    target_name = str(form.get("username", "")).strip()
    profile_user = get_user_by_username(target_name)

    safe_path = "/profile/" + quote(target_name, safe="")

    if profile_user is None:
        return RedirectResponse(
            add_query(APP_HOME, err="profile_not_found"),
            status_code=status.HTTP_302_FOUND,
        )

    follow_collection.delete_one(
        {
            "follower_id": user_token["user_id"],
            "following_id": profile_user["user_id"],
        }
    )

    return RedirectResponse(safe_path, status_code=status.HTTP_302_FOUND)


@app.post("/upload-profile-picture", response_class=RedirectResponse)
async def upload_profile_picture(request: Request):
    id_token = request.cookies.get("token")
    user_token = check_firebase_token(id_token)
    if not user_token:
        return RedirectResponse("/", status_code=status.HTTP_302_FOUND)

    viewer = get_user(user_token)
    if not has_username(viewer):
        return RedirectResponse("/set-username", status_code=status.HTTP_302_FOUND)

    form = await request.form()
    upload = form.get("photo")

    if upload is None:
        handle = str(viewer.get("username", "")).strip()
        dest = "/profile/" + quote(handle, safe="")
        return RedirectResponse(
            add_query(dest, err="photo_missing"),
            status_code=status.HTTP_302_FOUND,
        )

    raw_name = upload.filename
    content_type = upload.content_type
    file_bytes = await upload.read()

    allowed, _ = profile_picture_allowed(raw_name, content_type)
    handle = str(viewer.get("username", "")).strip()
    dest = "/profile/" + quote(handle, safe="")

    if not allowed:
        return RedirectResponse(
            add_query(dest, err="photo_bad_type"),
            status_code=status.HTTP_302_FOUND,
        )

    try:
        url = upload_profile_bytes(
            user_token["user_id"], raw_name, file_bytes, content_type
        )
    except Exception as e:
        print(e)
        return RedirectResponse(
            add_query(dest, err="photo_upload_failed"),
            status_code=status.HTTP_302_FOUND,
        )

    if url is None:
        return RedirectResponse(
            add_query(dest, err="photo_bad_type"),
            status_code=status.HTTP_302_FOUND,
        )

    user_collection.update_one(
        {"user_id": user_token["user_id"]},
        {"$set": {"profile_image_url": url}},
    )

    return RedirectResponse(
        add_query(dest, ok="photo_updated"),
        status_code=status.HTTP_302_FOUND,
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

    match = re.match(r"^[A-Za-z0-9_]{1,64}$", username)
    if match is None:
        return RedirectResponse(
            add_query("/set-username", err="username_invalid"),
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
