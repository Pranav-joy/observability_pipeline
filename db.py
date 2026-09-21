# These will be set on startup
_db = None


def init_db(db):
    global _db
    _db = db


async def count_users():
    return await _db["users"].count_documents({})


async def seed_users(users):
    await _db["users"].insert_many(users)


async def search_user(user_id, org_id):
    return await _db["users"].find_one({"user_id": user_id, "org_id": org_id})


async def insert_form_record(message, form_record_id, user_id):
    await _db["form_records"].insert_one({
        "message": message,
        "form_record_id": form_record_id,
        "user_id": user_id,
    })
    return form_record_id
