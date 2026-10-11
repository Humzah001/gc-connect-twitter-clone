# GC Connect

A Twitter-style social microblogging app built with FastAPI, using Firebase for
authentication, MongoDB for data, and Azure Blob Storage for image hosting.

Built as Assignment 2 for Cloud & Computing (Griffith University).

## Features

- Email/password sign-up and login via Firebase Authentication
- Username selection and editable profile bio
- Post tweets (280 characters) with optional image attachments
- Edit and delete your own tweets
- Retweet other users' posts
- Follow and unfollow users
- Profile pictures uploaded to Azure Blob Storage
- User search, which lists all users when the query is empty
- Home timeline showing the latest 20 tweets

## Tech stack

| Layer | Technology |
| --- | --- |
| Web framework | FastAPI + Starlette, Jinja2 templates |
| Authentication | Firebase Authentication (ID token verification) |
| Database | MongoDB (Atlas or local) |
| File storage | Azure Blob Storage (Azurite for local development) |
| Server | Uvicorn |

## Getting started

### 1. Install dependencies

```bash
python3 -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

### 2. Configure the database

```bash
cp .env.example .env
```

Then set `MONGO_URI` in `.env` to your MongoDB connection string — either a
MongoDB Atlas cluster or a local instance (`mongodb://127.0.0.1:27017`).
The app will refuse to start if this is not set.

### 3. Start Azure Blob Storage

Image uploads need a blob endpoint. For local development, run the Azurite
emulator, which the app is configured to use by default:

```bash
npx azurite --blobPort 10000 --queuePort 10001 --tablePort 10002
```

### 4. Run the app

```bash
uvicorn main:app --reload
```

The app is then available at http://127.0.0.1:8000.

## Configuration notes

- `MONGO_DB` in `main.py` sets the database name.
- `AZURE_CONNECTION_STRING` in `main.py` points at the Azurite emulator using
  its standard well-known development credentials. Replace it with a real
  connection string to deploy against Azure Blob Storage.
- The Firebase config in `static/firebase-login.js` contains a client-side web
  API key, which is designed to be public. Access is restricted through Firebase
  security rules and authorised domains, not by hiding this key.
