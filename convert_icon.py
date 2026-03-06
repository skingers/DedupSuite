from PIL import Image
import os

# 1. CHANGE THIS to your actual image filename!
source_file = "logo.png" 

if os.path.exists(source_file):
    img = Image.open(source_file)
    # Define standard Windows icon sizes
    icon_sizes = [(16, 16), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]
    # Save it
    img.save("app.ico", sizes=icon_sizes)
    print("✅ Success! 'app.ico' has been created.")
else:
    print(f"❌ Error: Could not find '{source_file}' in this folder.")