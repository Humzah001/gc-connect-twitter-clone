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
from bson import ObjectId
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
retweet_collection = db["retweets"]

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
    "edit_denied": "You cannot edit that tweet.",
    "edit_empty": "Tweet text cannot be empty.",
    "edit_long": "Tweets must be 280 characters or fewer.",
    "bio_long": "Bio must be 280 characters or fewer.",
    "tweet_image_bad_type": "Tweet images must be JPG or PNG.",
    "tweet_delete_denied": "You cannot delete that tweet.",
    "retweet_already": "You already retweeted this.",
    "retweet_invalid": "That tweet cannot be retweeted.",
}

FLASH_OK = {
    "photo_updated": "Profile picture updated.",
    "tweet_updated": "Tweet updated.",
    "bio_updated": "Bio saved.",
    "tweet_deleted": "Tweet deleted.",
    "retweet_added": "Retweet added.",
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
            "bio": "",
        }

        user_collection.insert_one(new_user)
        user = user_collection.find_one({"user_id": user_id})

    return user


def ensure_bio_field(user):
    if user is None:
        return user
    if "bio" not in user:
        user_collection.update_one(
            {"user_id": user["user_id"]},
            {"$set": {"bio": ""}},
        )
        user = user_collection.find_one({"user_id": user["user_id"]})
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


def get_following_user_ids(follower_id):
    out = []
    for row in follow_collection.find({"follower_id": follower_id}):
        fid = row.get("following_id")
        if fid is None:
            continue
        seen = False
        for x in out:
            if x == fid:
                seen = True
                break
        if not seen:
            out.append(fid)
    return out


def id_in_list(uid, id_list):
    for x in id_list:
        if x == uid:
            return True
    return False


def sort_feed_entries_by_time(entries):
    n = len(entries)
    for i in range(n):
        for j in range(i + 1, n):
            ti = entries[i].get("sort_time", datetime.min)
            tj = entries[j].get("sort_time", datetime.min)
            if ti < tj:
                temp = entries[i]
                entries[i] = entries[j]
                entries[j] = temp
    return entries


def user_has_retweeted(me_user_id, original_tweet_oid):
    row = retweet_collection.find_one(
        {
            "retweeter_id": me_user_id,
            "original_tweet_id": original_tweet_oid,
        }
    )
    return row is not None


def delete_tweet_image_blob(tweet_oid_str):
    blob_name = "tweet-images/" + tweet_oid_str + "/image"
    blob_client = azure_container_client.get_blob_client(blob_name)
    try:
        blob_client.delete_blob()
    except Exception:
        pass


def upload_tweet_image_blob(tweet_oid_str, filename, file_bytes, content_type):
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
    blob_name = "tweet-images/" + tweet_oid_str + "/image"
    blob_client = azure_container_client.get_blob_client(blob_name)
    blob_client.upload_blob(
        file_bytes,
        overwrite=True,
        content_settings=ContentSettings(content_type=upload_type),
    )
    return blob_client.url


def get_author_username(uid):
    author = user_collection.find_one({"user_id": uid})
    if author is None:
        return "unknown"
    name = author.get("username")
    if name is None:
        return "unknown"
    return str(name)


def get_timeline_rows(me_user_id, limit):
    author_ids = [me_user_id]
    for fid in get_following_user_ids(me_user_id):
        if not id_in_list(fid, author_ids):
            author_ids.append(fid)

    entries = []

    for t in tweet_collection.find({}):
        uid = t.get("user_id")
        if not id_in_list(uid, author_ids):
            continue
        tw_id = t.get("_id")
        tweet_id_str = str(tw_id) if tw_id is not None else ""
        entries.append(
            {
                "kind": "tweet",
                "tweet": t,
                "tweet_id": tweet_id_str,
                "author_username": get_author_username(uid),
                "sort_time": t.get("created_at", datetime.min),
                "retweeter_username": None,
            }
        )

    for r in retweet_collection.find({}):
        rt_uid = r.get("retweeter_id")
        if not id_in_list(rt_uid, author_ids):
            continue
        orig_id = r.get("original_tweet_id")
        if orig_id is None:
            continue
        orig = tweet_collection.find_one({"_id": orig_id})
        if orig is None:
            continue
        ouid = orig.get("user_id")
        rtw_name = get_author_username(rt_uid)
        o_tw_id = orig.get("_id")
        o_tid_str = str(o_tw_id) if o_tw_id is not None else ""
        entries.append(
            {
                "kind": "retweet",
                "tweet": orig,
                "tweet_id": o_tid_str,
                "author_username": get_author_username(ouid),
                "sort_time": r.get("created_at", datetime.min),
                "retweeter_username": rtw_name,
            }
        )

    entries = sort_feed_entries_by_time(entries)

    rows = []
    i = 0
    while i < len(entries) and i < limit:
        e = entries[i]
        tw = e["tweet"]
        oid = None
        try:
            oid = ObjectId(e["tweet_id"])
        except Exception:
            oid = None
        already_rt = False
        if oid is not None:
            already_rt = user_has_retweeted(me_user_id, oid)
        own_tweet = tw.get("user_id") == me_user_id
        rows.append(
            {
                "kind": e.get("kind", "tweet"),
                "tweet_id": e["tweet_id"],
                "tweet": tw,
                "author_username": e["author_username"],
                "is_own": own_tweet,
                "retweeter_username": e.get("retweeter_username"),
                "already_retweeted": already_rt,
            }
        )
        i = i + 1

    return rows


def get_profile_feed_rows(profile_user_id, limit):
    entries = []

    for t in tweet_collection.find({"user_id": profile_user_id}):
        tw_id = t.get("_id")
        tid_str = str(tw_id) if tw_id is not None else ""
        entries.append(
            {
                "kind": "tweet",
                "tweet": t,
                "tweet_id": tid_str,
                "author_username": get_author_username(profile_user_id),
                "sort_time": t.get("created_at", datetime.min),
                "retweeter_username": None,
            }
        )

    for r in retweet_collection.find({"retweeter_id": profile_user_id}):
        orig_id = r.get("original_tweet_id")
        if orig_id is None:
            continue
        orig = tweet_collection.find_one({"_id": orig_id})
        if orig is None:
            continue
        ouid = orig.get("user_id")
        o_tw_id = orig.get("_id")
        o_tid_str = str(o_tw_id) if o_tw_id is not None else ""
        uname = get_author_username(profile_user_id)
        entries.append(
            {
                "kind": "retweet",
                "tweet": orig,
                "tweet_id": o_tid_str,
                "author_username": get_author_username(ouid),
                "sort_time": r.get("created_at", datetime.min),
                "retweeter_username": uname,
            }
        )

    entries = sort_feed_entries_by_time(entries)

    out = []
    j = 0
    while j < len(entries) and j < limit:
        e = entries[j]
        out.append(
            {
                "kind": e.get("kind", "tweet"),
                "tweet_id": e["tweet_id"],
                "tweet": e["tweet"],
                "author_username": e["author_username"],
                "retweeter_username": e.get("retweeter_username"),
            }
        )
        j = j + 1

    return out


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
    user = ensure_bio_field(user)

    if not has_username(user):
        return RedirectResponse(
            "/set-username",
            status_code=status.HTTP_302_FOUND,
        )

    flash_message, flash_kind = get_flash_message(request)
    timeline_rows = get_timeline_rows(user_token["user_id"], 20)

    return templates.TemplateResponse(
        "home.html",
        {
            "request": request,
            "app_title": APP_TITLE,
            "user_token": user_token,
            "user_info": user,
            "flash_message": flash_message,
            "flash_kind": flash_kind,
            "timeline_rows": timeline_rows,
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
                "profile_feed": [],
                "is_own_profile": False,
                "is_following": False,
                "flash_message": flash_message,
                "flash_kind": flash_kind,
                "max_tweet_len": MAX_TWEET_LEN,
            },
        )

    profile_user = ensure_profile_image_field(profile_user)
    profile_user = ensure_bio_field(profile_user)

    raw_feed = get_profile_feed_rows(profile_user["user_id"], 10)
    viewer_uid = None
    if user_token is not None and viewer is not None:
        viewer_uid = user_token["user_id"]

    profile_feed = []
    for row in raw_feed:
        r = dict(row)
        oid = None
        try:
            oid = ObjectId(row["tweet_id"])
        except Exception:
            oid = None
        r["already_retweeted"] = False
        if viewer_uid is not None and oid is not None:
            r["already_retweeted"] = user_has_retweeted(viewer_uid, oid)
        tw_uid = row["tweet"].get("user_id")
        r["is_own_original"] = tw_uid == profile_user["user_id"]
        profile_feed.append(r)

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
            "profile_feed": profile_feed,
            "is_own_profile": is_own_profile,
            "is_following": following_flag,
            "flash_message": flash_message,
            "flash_kind": flash_kind,
            "max_tweet_len": MAX_TWEET_LEN,
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

    tweet_doc = {
        "user_id": user_token["user_id"],
        "text": text,
        "created_at": datetime.utcnow(),
    }

    ins = tweet_collection.insert_one(tweet_doc)
    new_id = ins.inserted_id
    tid_str = str(new_id)

    upl = form.get("tweet_image")
    fn = ""
    if upl is not None:
        fn = getattr(upl, "filename", "") or ""
        fn = str(fn).strip()

    if fn != "":
        raw_name = upl.filename
        content_type = upl.content_type
        file_bytes = await upl.read()
        allowed, _ = profile_picture_allowed(raw_name, content_type)
        if not allowed:
            tweet_collection.delete_one({"_id": new_id})
            return RedirectResponse(
                add_query(APP_HOME, err="tweet_image_bad_type"),
                status_code=status.HTTP_302_FOUND,
            )
        try:
            url = upload_tweet_image_blob(
                tid_str, raw_name, file_bytes, content_type
            )
        except Exception as e:
            print(e)
            tweet_collection.delete_one({"_id": new_id})
            return RedirectResponse(
                add_query(APP_HOME, err="photo_upload_failed"),
                status_code=status.HTTP_302_FOUND,
            )
        if url is None:
            tweet_collection.delete_one({"_id": new_id})
            return RedirectResponse(
                add_query(APP_HOME, err="tweet_image_bad_type"),
                status_code=status.HTTP_302_FOUND,
            )
        tweet_collection.update_one(
            {"_id": new_id},
            {"$set": {"image_url": url}},
        )

    return RedirectResponse(APP_HOME, status_code=status.HTTP_302_FOUND)


@app.get("/edit-tweet", response_class=HTMLResponse)
async def edit_tweet_page(request: Request):
    id_token = request.cookies.get("token")
    user_token = check_firebase_token(id_token)
    if not user_token:
        return RedirectResponse("/", status_code=status.HTTP_302_FOUND)

    user = get_user(user_token)
    if not has_username(user):
        return RedirectResponse("/set-username", status_code=status.HTTP_302_FOUND)

    tweet_id_raw = request.query_params.get("tweet_id", "").strip()
    oid = None
    try:
        oid = ObjectId(tweet_id_raw)
    except Exception:
        oid = None

    if oid is None:
        return RedirectResponse(
            add_query(APP_HOME, err="edit_denied"),
            status_code=status.HTTP_302_FOUND,
        )

    tweet = tweet_collection.find_one({"_id": oid})
    if tweet is None:
        return RedirectResponse(
            add_query(APP_HOME, err="edit_denied"),
            status_code=status.HTTP_302_FOUND,
        )
    if tweet.get("user_id") != user_token["user_id"]:
        return RedirectResponse(
            add_query(APP_HOME, err="edit_denied"),
            status_code=status.HTTP_302_FOUND,
        )

    flash_message, flash_kind = get_flash_message(request)
    user = ensure_profile_image_field(user)
    user = ensure_bio_field(user)

    return templates.TemplateResponse(
        "edit-tweet.html",
        {
            "request": request,
            "app_title": APP_TITLE,
            "user_token": user_token,
            "user_info": user,
            "tweet": tweet,
            "tweet_id": tweet_id_raw,
            "flash_message": flash_message,
            "flash_kind": flash_kind,
            "max_tweet_len": MAX_TWEET_LEN,
        },
    )


@app.post("/edit-tweet", response_class=RedirectResponse)
async def edit_tweet_save(request: Request):
    id_token = request.cookies.get("token")
    user_token = check_firebase_token(id_token)
    if not user_token:
        return RedirectResponse("/", status_code=status.HTTP_302_FOUND)

    user = get_user(user_token)
    if not has_username(user):
        return RedirectResponse("/set-username", status_code=status.HTTP_302_FOUND)

    form = await request.form()
    tweet_id_raw = str(form.get("tweet_id", "")).strip()
    oid = None
    try:
        oid = ObjectId(tweet_id_raw)
    except Exception:
        oid = None

    if oid is None:
        return RedirectResponse(
            add_query(APP_HOME, err="edit_denied"),
            status_code=status.HTTP_302_FOUND,
        )

    tweet = tweet_collection.find_one({"_id": oid})
    if tweet is None or tweet.get("user_id") != user_token["user_id"]:
        return RedirectResponse(
            add_query(APP_HOME, err="edit_denied"),
            status_code=status.HTTP_302_FOUND,
        )

    text = form.get("text", "")
    text = str(text).strip()

    if not text:
        return RedirectResponse(
            add_query("/edit-tweet", tweet_id=tweet_id_raw, err="edit_empty"),
            status_code=status.HTTP_302_FOUND,
        )

    if len(text) > MAX_TWEET_LEN:
        return RedirectResponse(
            add_query("/edit-tweet", tweet_id=tweet_id_raw, err="edit_long"),
            status_code=status.HTTP_302_FOUND,
        )

    rm_raw = form.get("remove_image")
    remove_image = rm_raw == "on" or rm_raw == "1" or rm_raw == "true"

    upl = form.get("tweet_image")
    fn = ""
    if upl is not None:
        fn = getattr(upl, "filename", "") or ""
        fn = str(fn).strip()

    if remove_image:
        delete_tweet_image_blob(tweet_id_raw)

    if fn != "":
        raw_name = upl.filename
        content_type = upl.content_type
        file_bytes = await upl.read()
        allowed, _ = profile_picture_allowed(raw_name, content_type)
        if not allowed:
            return RedirectResponse(
                add_query("/edit-tweet", tweet_id=tweet_id_raw, err="tweet_image_bad_type"),
                status_code=status.HTTP_302_FOUND,
            )
        try:
            url = upload_tweet_image_blob(
                tweet_id_raw, raw_name, file_bytes, content_type
            )
        except Exception as e:
            print(e)
            return RedirectResponse(
                add_query("/edit-tweet", tweet_id=tweet_id_raw, err="photo_upload_failed"),
                status_code=status.HTTP_302_FOUND,
            )
        if url is None:
            return RedirectResponse(
                add_query("/edit-tweet", tweet_id=tweet_id_raw, err="tweet_image_bad_type"),
                status_code=status.HTTP_302_FOUND,
            )
        tweet_collection.update_one(
            {"_id": oid, "user_id": user_token["user_id"]},
            {"$set": {"text": text, "image_url": url}},
        )
    elif remove_image:
        tweet_collection.update_one(
            {"_id": oid, "user_id": user_token["user_id"]},
            {"$set": {"text": text}, "$unset": {"image_url": ""}},
        )
    else:
        tweet_collection.update_one(
            {"_id": oid, "user_id": user_token["user_id"]},
            {"$set": {"text": text}},
        )

    return RedirectResponse(
        add_query(APP_HOME, ok="tweet_updated"),
        status_code=status.HTTP_302_FOUND,
    )


@app.post("/delete-tweet", response_class=RedirectResponse)
async def delete_tweet_route(request: Request):
    id_token = request.cookies.get("token")
    user_token = check_firebase_token(id_token)
    if not user_token:
        return RedirectResponse("/", status_code=status.HTTP_302_FOUND)

    user = get_user(user_token)
    if not has_username(user):
        return RedirectResponse("/set-username", status_code=status.HTTP_302_FOUND)

    form = await request.form()
    tweet_id_raw = str(form.get("tweet_id", "")).strip()
    oid = None
    try:
        oid = ObjectId(tweet_id_raw)
    except Exception:
        oid = None

    if oid is None:
        return RedirectResponse(
            add_query(APP_HOME, err="tweet_delete_denied"),
            status_code=status.HTTP_302_FOUND,
        )

    tweet = tweet_collection.find_one({"_id": oid})
    if tweet is None or tweet.get("user_id") != user_token["user_id"]:
        return RedirectResponse(
            add_query(APP_HOME, err="tweet_delete_denied"),
            status_code=status.HTTP_302_FOUND,
        )

    delete_tweet_image_blob(tweet_id_raw)
    retweet_collection.delete_many({"original_tweet_id": oid})
    tweet_collection.delete_one({"_id": oid})

    return RedirectResponse(
        add_query(APP_HOME, ok="tweet_deleted"),
        status_code=status.HTTP_302_FOUND,
    )


@app.post("/retweet", response_class=RedirectResponse)
async def retweet_route(request: Request):
    id_token = request.cookies.get("token")
    user_token = check_firebase_token(id_token)
    if not user_token:
        return RedirectResponse("/", status_code=status.HTTP_302_FOUND)

    user = get_user(user_token)
    if not has_username(user):
        return RedirectResponse("/set-username", status_code=status.HTTP_302_FOUND)

    form = await request.form()
    raw_id = str(form.get("original_tweet_id", "")).strip()
    oid = None
    try:
        oid = ObjectId(raw_id)
    except Exception:
        oid = None

    if oid is None:
        return RedirectResponse(
            add_query(APP_HOME, err="retweet_invalid"),
            status_code=status.HTTP_302_FOUND,
        )

    orig = tweet_collection.find_one({"_id": oid})
    if orig is None:
        return RedirectResponse(
            add_query(APP_HOME, err="retweet_invalid"),
            status_code=status.HTTP_302_FOUND,
        )

    if user_has_retweeted(user_token["user_id"], oid):
        return RedirectResponse(
            add_query(APP_HOME, err="retweet_already"),
            status_code=status.HTTP_302_FOUND,
        )

    retweet_collection.insert_one(
        {
            "retweeter_id": user_token["user_id"],
            "original_tweet_id": oid,
            "created_at": datetime.utcnow(),
        }
    )

    return RedirectResponse(
        add_query(APP_HOME, ok="retweet_added"),
        status_code=status.HTTP_302_FOUND,
    )


@app.post("/save-bio", response_class=RedirectResponse)
async def save_bio(request: Request):
    id_token = request.cookies.get("token")
    user_token = check_firebase_token(id_token)
    if not user_token:
        return RedirectResponse("/", status_code=status.HTTP_302_FOUND)

    viewer = get_user(user_token)
    if not has_username(viewer):
        return RedirectResponse("/set-username", status_code=status.HTTP_302_FOUND)

    form = await request.form()
    raw_bio = form.get("bio")
    if raw_bio is None:
        raw_bio = ""
    bio = str(raw_bio).strip()

    if len(bio) > MAX_TWEET_LEN:
        handle = str(viewer.get("username", "")).strip()
        dest = "/profile/" + quote(handle, safe="")
        return RedirectResponse(
            add_query(dest, err="bio_long"),
            status_code=status.HTTP_302_FOUND,
        )

    user_collection.update_one(
        {"user_id": user_token["user_id"]},
        {"$set": {"bio": bio}},
    )

    handle = str(viewer.get("username", "")).strip()
    dest = "/profile/" + quote(handle, safe="")
    return RedirectResponse(
        add_query(dest, ok="bio_updated"),
        status_code=status.HTTP_302_FOUND,
    )
