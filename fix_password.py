#!/usr/bin/env python3
"""
Script to fix and regenerate password hashes for users.
Use this to reset a user's password to a known value.
"""

import os
import sys
from werkzeug.security import generate_password_hash

# Add the app root to path
sys.path.insert(0, os.path.dirname(__file__))

from app import app
from models import db, Utente

def fix_user_password(username, new_password):
    """
    Find a user by username and set their password to a new value.
    """
    with app.app_context():
        user = Utente.query.filter_by(username=username).first()
        
        if not user:
            print(f"❌ User '{username}' not found in database")
            return False
        
        # Generate correct werkzeug hash
        correct_hash = generate_password_hash(new_password)
        
        print(f"✓ Found user: {user.nome_cognome} ({user.email})")
        print(f"✓ Old hash: {user.password_hash[:50]}...")
        print(f"✓ New hash: {correct_hash[:50]}...")
        
        # Update
        user.password_hash = correct_hash
        db.session.commit()
        
        print(f"✅ Password updated successfully!")
        print(f"   Username: {username}")
        print(f"   New password: {new_password}")
        
        # Verify it works
        if user.check_password(new_password):
            print(f"✅ Password verification: SUCCESS")
            return True
        else:
            print(f"❌ Password verification: FAILED")
            return False

if __name__ == '__main__':
    if len(sys.argv) < 3:
        print("Usage: python fix_password.py <username> <new_password>")
        print("\nExample:")
        print("  python fix_password.py prenotazioni CambiaMi2026!")
        sys.exit(1)
    
    username = sys.argv[1]
    new_password = sys.argv[2]
    
    success = fix_user_password(username, new_password)
    sys.exit(0 if success else 1)