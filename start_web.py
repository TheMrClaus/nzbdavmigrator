#!/usr/bin/env python3
"""
Quick start script for NZBDAVMigrator Web Interface
"""

import sys
import os

def main():
    # Ensure we're in the right directory
    script_dir = os.path.dirname(os.path.abspath(__file__))
    os.chdir(script_dir)

    # Check if required files exist
    if not os.path.exists('export_nzb.py'):
        print("Error: export_nzb.py not found in current directory")
        sys.exit(1)

    if not os.path.exists('nzbdav_web.py'):
        print("Error: nzbdav_web.py not found in current directory")
        sys.exit(1)

    # Import and run the web application
    try:
        from nzbdav_web import main as web_main
        web_main()
    except ImportError as e:
        print(f"Error importing required modules: {e}")
        print("Make sure all dependencies are installed")
        sys.exit(1)
    except KeyboardInterrupt:
        print("\nGoodbye!")
        sys.exit(0)

if __name__ == "__main__":
    main()