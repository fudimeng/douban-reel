"""Generate a local administrator credential without overwriting existing configuration."""
import os
import secrets
from pathlib import Path

path = Path(__file__).resolve().parents[1] / '.env'
try:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
except FileExistsError:
    raise SystemExit('.env 已存在，未覆盖。管理员账号与密码可在该文件中查看。')
with os.fdopen(descriptor, 'w') as file:
    file.write(f'AUTH_ENABLED=true\nADMIN_USERNAME=admin\nADMIN_PASSWORD={secrets.token_urlsafe(24)}\nBIND_ADDRESS=127.0.0.1\nPORT=8787\n')
print('已生成 .env。请在该文件查看登录密码，然后运行 docker compose up -d --build。')
