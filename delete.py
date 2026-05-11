
import zipfile, os

project_dir = r'D:\personal\ai_projects\56.wechat_collection'
zip_path = r'D:\personal\ai_projects\56.wechat_collection\deploy.zip'

if os.path.exists(zip_path):
    os.remove(zip_path)

skip_dirs = {'__pycache__', '.git', 'venv', 'env', 'packages'}

with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zf:
    for root, dirs, files in os.walk(project_dir):
        dirs[:] = [d for d in dirs if d not in skip_dirs]
        for file in files:
            if file == 'deploy.zip':
                continue
            filepath = os.path.join(root, file)
            arcname = os.path.relpath(filepath, project_dir)
            zf.write(filepath, arcname)
            print(f'  Added: {arcname}')

size = os.path.getsize(zip_path) / 1024
print(f'Done! deploy.zip size: {size:.1f} KB')