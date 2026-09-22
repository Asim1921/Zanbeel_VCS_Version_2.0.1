#!/usr/bin/env python3
import sqlite3

db = sqlite3.connect('/home/aiuser/FoxNest/server/foxnest.db')
cursor = db.cursor()

users = cursor.execute('SELECT * FROM users ORDER BY id').fetchall()

print("=" * 100)
print("FOXNEST DATABASE - ALL 8 USERS WITH CREDENTIALS")
print("=" * 100)
print()

for user in users:
    user_id, username, email, full_name, role, team_lead_id, created, updated, active, password_hash = user
    
    print(f"USER #{user_id}")
    print(f"  Username:      {username}")
    print(f"  Email:         {email or 'N/A'}")
    print(f"  Full Name:     {full_name or 'N/A'}")
    print(f"  Role:          {role}")
    print(f"  Team Lead ID:  {team_lead_id or 'None'}")
    print(f"  Active:        {bool(active)}")
    print(f"  Created:       {created}")
    print(f"  Password Hash: {password_hash}")
    print("-" * 100)

db.close()

print()
print("⚠️  IMPORTANT NOTES:")
print("=" * 100)
print("1. Passwords are stored as PBKDF2-SHA256 hashes (200,000 iterations)")
print("2. These hashes CANNOT be reversed to get original passwords")
print("3. This is industry-standard secure password storage")
print("4. To login, user enters password → system hashes it → compares to stored hash")
print("5. If you forgot a password, you must RESET it, not retrieve it")
print("=" * 100)
