import os
import subprocess as sp


def reprocess_images(root_dir):
    for dirpath, dirnames, filenames in os.walk(root_dir):
        for fname in filenames:
            if not fname.lower().endswith(('.jpg', '.jpeg', '.png', '.gif')):
                continue

            fpath = os.path.join(dirpath, fname)
            try:
                old_size = os.stat(fpath).st_size
                sp.run(['mogrify', '-quality', '75', fpath], check=True)
                new_size = os.stat(fpath).st_size
                kb_saved = (old_size - new_size) / 1024
                if kb_saved > 20:
                    sp.run(['git', 'add', fpath], check=True)
                    print(f"updated  : {fpath:<100} {kb_saved:>9.3f} kb saved")
                else:
                    sp.run(['git', 'checkout', fpath], check=True, capture_output=True)
                    # print(f"unchanged: {fpath}")
            except subprocess.CalledProcessError as e:
                print(f"Error processing {fpath}: {e}")

# Call the function to reprocess images
reprocess_images('images/2024/05')
