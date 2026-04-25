import os
import subprocess
import sys
from pathlib import Path

def bundle_dependencies():
    """
    Downloads necessary dependencies for the backend into the 'vendor' folder.
    This allows the backend to run on bastions without internet access.
    """
    backend_dir = Path(__file__).parent / "beta3" / "backend"
    vendor_dir = backend_dir / "vendor"
    
    # Ensure vendor dir exists
    os.makedirs(vendor_dir, exist_ok=True)
    
    print(f"Bundling dependencies into {vendor_dir}...")
    
    dependencies = [
        "fastapi",
        "uvicorn",
        "websockets",
        "starlette",
        "pydantic",
        "typing-extensions",
        "anyio",
        "h11",
        "click"
    ]
    
    try:
        # We use pip download to get the wheels without installing them
        subprocess.check_call([
            sys.executable, "-m", "pip", "download",
            "-d", str(vendor_dir),
            *dependencies
        ])
        print("\nSuccess! Dependencies downloaded.")
        print("To use these on the bastion, the backend will need to add this folder to sys.path.")
    except Exception as e:
        print(f"Error bundling dependencies: {e}")

if __name__ == "__main__":
    bundle_dependencies()
