# File Deduplicator Suite

A professional, multi-threaded GUI application for identifying, reviewing, and managing duplicate files. It features both exact byte-matching and visual similarity detection for media files, wrapped in a modern, theme-aware interface.

## Features

### Core Functionality
*   **Dual Scanning Engines:**
    *   **Exact Match:** Identifies bit-for-bit duplicates using SHA256 hashing.
    *   **Visual/Video:** Uses perceptual hashing (pHash) to detect similar images and video frames, robust against resizing or re-encoding.
*   **Folder Merger:** A dedicated tool to merge an "Incoming" directory into a "Master" directory, automatically handling collisions and duplicates.

### Review & Management
*   **Side-by-Side Preview:** Visually compare original and duplicate files (supports Images and Video frames).
*   **Smart Select:** Heuristic algorithm to automatically select lower-quality or smaller files for deletion.
*   **Safety First:** Undo stack for file operations and "Move" functionality to quarantine duplicates before deletion.
*   **Find Similar:** Search within results for files visually similar to the selected duplicate.

### User Experience
*   **Modern UI:** Clean interface with vector icons and toggleable **Dark/Light themes**.
*   **Performance:** Configurable multi-threading to utilize multi-core CPUs.
*   **Reporting:** Export audit results to **PDF** or **CSV**.
*   **Customization:** Ignore specific file extensions or folders.

## Installation

### Prerequisites
*   Python 3.8 or higher

### Option 1: Using pip (Standard)

1.  Clone the repository:
    ```bash
    git clone https://github.com/yourusername/dedup-suite.git
    cd dedup-suite
    ```
2.  Install dependencies:
    ```bash
    pip install -r requirements.txt
    ```
    *Alternatively:*
    ```bash
    pip install Pillow opencv-python-headless imagehash reportlab numpy
    ```

### Option 2: Using uv (Fast)

If you use [uv](https://github.com/astral-sh/uv) for package management:

```bash
uv pip install -r requirements.txt
```

*Note: `reportlab` is optional but required for PDF export functionality.*

## Usage

### Running from Source

Navigate to the directory containing the script and run:

```bash
python dedup_suite.py
```

### Building an Executable

To create a standalone `.exe` for Windows distribution, use PyInstaller:

```bash
pip install pyinstaller
pyinstaller --noconsole --onefile --name="DedupSuite" --icon="app.ico" --add-data="app.ico;." dedup_suite.py
```

*(Ensure you have an `app.ico` file in the directory, or remove the icon arguments).*

## How to Use

1.  **Audit / Dedup Tab:**
    *   Select a **Source** directory.
    *   Choose **Exact** or **Visual/Video** mode.
    *   Click **Start Scan**.
    *   If **Review Mode** is checked, a window will pop up allowing you to process duplicates.
2.  **Review Window:**
    *   Use **Smart Select** to auto-mark duplicates based on file size.
    *   Use **Move** to relocate duplicates to a specific folder.
    *   Use **Delete Duplicate** to remove the file on the right.
3.  **Merge Folders Tab:**
    *   Select a **Master** (destination) and **Incoming** (source) folder.
    *   Run a **Dry Run** first to see what will happen in the logs.

## Contributing

Contributions are welcome! Please follow these steps:

1.  Fork the repository.
2.  Create a feature branch (`git checkout -b feature/NewFeature`).
3.  Commit your changes and open a Pull Request.