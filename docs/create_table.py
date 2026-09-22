#!/usr/bin/env python3
"""
Create IssueAccessRequest table in FoxNest database
"""

import sys
import os

# Add server directory to path
sys.path.insert(0, '/home/aiuser/FoxNest/server')

try:
    from database.database import engine, Base
    from database.models import IssueAccessRequest
    
    print("=" * 60)
    print("Creating IssueAccessRequest table...")
    print("=" * 60)
    
    # Create the table
    Base.metadata.create_all(bind=engine)
    
    print("✓ SUCCESS: IssueAccessRequest table created")
    print("=" * 60)
    print("\nDatabase Information:")
    print(f"  Database File: /home/aiuser/FoxNest/server/foxnest.db")
    print(f"  Table Name: issue_access_requests")
    print(f"  Features Enabled:")
    print(f"    ✓ Issue access request tracking")
    print(f"    ✓ Admin approval/denial workflow")
    print(f"    ✓ Automatic @mention handling")
    print("=" * 60)
    print("\nNext steps:")
    print("  1. Restart backend service: sudo systemctl restart foxnest-backend")
    print("  2. Verify: curl http://localhost:33333/api/")
    print("  3. Create issue with @mention to test feature")
    print("=" * 60)
    
except Exception as e:
    print(f"✗ ERROR: Failed to create table")
    print(f"  {str(e)}")
    sys.exit(1)
