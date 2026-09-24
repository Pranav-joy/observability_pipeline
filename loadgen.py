import json
import os
import random
import time
import sys

import httpx

API_URL = os.getenv("API_URL", "http://api:8000")

with open("users.json") as f:
    USERS = json.load(f)


def wait_for_api(client, retries=30, delay=2.0):
    for i in range(retries):
        try:
            resp = client.get(f"{API_URL}/health", timeout=2.0)
            if resp.status_code == 200:
                print(f"API ready at {API_URL}")
                sys.stdout.flush()
                return True
        except Exception as e:
            print(f"waiting for API ({i + 1}/{retries}): {e}")
            sys.stdout.flush()
        time.sleep(delay)
    return False


def signin(client, user):
    resp = client.post(f"{API_URL}/signin", json=user)
    resp.raise_for_status()
    return resp.json()["access_token"]


def call_form_a(client, token):
    resp = client.post(
        f"{API_URL}/form_A",
        headers={"Authorization": f"Bearer {token}"},
    )
    return resp.status_code


def call_form_b(client, token):
    resp = client.post(
        f"{API_URL}/form_B",
        headers={"Authorization": f"Bearer {token}"},
    )
    return resp.status_code


def call_external(client, token):
    resp = client.post(
        f"{API_URL}/external",
        headers={"Authorization": f"Bearer {token}"},
    )
    return resp.status_code


ENDPOINTS = [call_form_a, call_form_b, call_external]


def main():
    print(f"Load generator starting — {len(USERS)} users, hitting {API_URL}")
    sys.stdout.flush()

    with httpx.Client(timeout=10.0) as client:
        if not wait_for_api(client):
            print("API never became ready, exiting")
            sys.stdout.flush()
            sys.exit(1)

        while True:
            user = random.choice(USERS)
            try:
                token = signin(client, user)
            except Exception as e:
                print(f"signin failed for {user}: {e}")
                sys.stdout.flush()
                time.sleep(2)
                continue

            for _ in range(random.randint(3, 8)):
                endpoint = random.choice(ENDPOINTS)
                try:
                    status = endpoint(client, token)
                    print(f"{user['user_id']:>10}  {endpoint.__name__}  {status}")
                except Exception as e:
                    print(f"{user['user_id']:>10}  {endpoint.__name__}  ERROR {e}")
                sys.stdout.flush()
                time.sleep(random.uniform(0.5, 2.5))

            time.sleep(random.uniform(1, 4))


if __name__ == "__main__":
    main()
