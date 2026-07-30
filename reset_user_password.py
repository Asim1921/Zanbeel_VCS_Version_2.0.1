#!/usr/bin/env python3
"""
Password Reset Script for FoxNest Users
Usage: python3 reset_user_password.py <username> <new_password>
"""

import sys
import sqlite3
from werkzeug.security import generate_password_hash

def reset_password(username, new_password):
    """Reset password for a user"""
    
    db = sqlite3.connect('/home/aiuser/FoxNest/server/foxnest.db')
    cursor = db.cursor()
    
    try:
        # Check if user exists
        user = cursor.execute('SELECT id, username, role FROM users WHERE username = ?', (username,)).fetchone()
        
        if not user:
            print(f"❌ ERROR: User '{username}' not found!")
            print("\nAvailable users:")
            users = cursor.execute('SELECT username FROM users ORDER BY id').fetchall()
            for u in users:
                print(f"  - {u[0]}")
            return False
        
        user_id, username, role = user
        
        # Generate new password hash (pbkdf2:sha256 with 200000 iterations)
        new_hash = generate_password_hash(new_password, method='pbkdf2:sha256', salt_length=16)
        
        # Update password
        cursor.execute('UPDATE users SET password_hash = ?, updated_at = CURRENT_TIMESTAMP WHERE username = ?', 
                      (new_hash, username))
        db.commit()
        
        print("=" * 80)
        print("✅ PASSWORD RESET SUCCESSFUL!")
        print("=" * 80)
        print(f"User ID:       {user_id}")
        print(f"Username:      {username}")
        print(f"Role:          {role}")
        print(f"New Password:  {new_password}")
        print(f"New Hash:      {new_hash[:80]}...")
        print("=" * 80)
        print("\n🔐 You can now login with:")
        print(f"   Username: {username}")
        print(f"   Password: {new_password}")
        print("=" * 80)
        
        return True
        
    except Exception as e:
        print(f"❌ ERROR: {e}")
        db.rollback()
        return False
    finally:
        db.close()

if __name__ == "__main__":
    if len(sys.argv) != 3:
        print("=" * 80)
        print("FoxNest Password Reset Tool")
        print("=" * 80)
        print("\nUsage:")
        print(f"  python3 {sys.argv[0]} <username> <new_password>\n")
        print("Examples:")
        print(f"  python3 {sys.argv[0]} bilal8244 MyNewPassword123")
        print(f"  python3 {sys.argv[0]} Jalal admin123")
        print("\nAvailable users:")
        
        db = sqlite3.connect('/home/aiuser/FoxNest/server/foxnest.db')
        cursor = db.cursor()
        users = cursor.execute('SELECT id, username, role FROM users ORDER BY id').fetchall()
        for user_id, username, role in users:
            print(f"  {user_id}. {username} ({role})")
        db.close()
        
        print("=" * 80)
        sys.exit(1)
    
    username = sys.argv[1]
    new_password = sys.argv[2]
    
    if len(new_password) < 4:
        print("❌ ERROR: Password too short! Use at least 4 characters.")
        sys.exit(1)
    
    success = reset_password(username, new_password)
    sys.exit(0 if success else 1)
