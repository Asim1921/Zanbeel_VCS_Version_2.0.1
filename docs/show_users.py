#!/usr/bin/env python3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "server"))

from database.database import SessionLocal
from database.models import User

db = SessionLocal()

users = db.query(User).all()

print("=" * 80)
print(f"FOXNEST DATABASE - {len(users)} USERS")
print("=" * 80)

for i, user in enumerate(users, 1):
    print(f"\n{i}. USERNAME: {user.username}")
    print(f"   Email: {user.email or 'N/A'}")
    print(f"   Full Name: {user.full_name or 'N/A'}")
    print(f"   Role: {user.role}")
    print(f"   Active: {user.is_active}")
    print(f"   Password Hash: {user.hashed_password if hasattr(user, 'hashed_password') else 'NO PASSWORD FIELD'}")
    print(f"   Team Lead ID: {user.team_lead_id or 'None'}")
    print(f"   Created: {user.created_at}")

db.close()

print("\n" + "=" * 80)
print("NOTE: Currently NO PASSWORD AUTHENTICATION is implemented.")
print("Users are identified by username only (no login required).")
print("=" * 80)
