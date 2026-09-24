# -*- coding: utf-8 -*-
"""make_version.py —— 生成 PyInstaller 用的版本资源文件"""
import io
import os
import time

HERE = os.path.dirname(os.path.abspath(__file__))
VERSION = (1, 2, 0, 0)
VSTR = ".".join(str(x) for x in VERSION)

CONTENT = f'''# UTF-8
#
# 完美世界扫号工具 —— Windows 版本资源
#
VSVersionInfo(
  ffi=FixedFileInfo(
    filevers={VERSION},
    prodvers={VERSION},
    mask=0x3f,
    flags=0x0,
    OS=0x40004,
    fileType=0x1,
    subtype=0x0,
    date=(0, 0)
  ),
  kids=[
    StringFileInfo([
      StringTable(
        '080404B0',
        [StringStruct('CompanyName', '内部工具'),
         StringStruct('FileDescription', '完美世界 · 查区查等级 批量扫号工具'),
         StringStruct('FileVersion', '{VSTR}'),
         StringStruct('InternalName', 'wm_scan'),
         StringStruct('LegalCopyright', '仅供内部授权使用'),
         StringStruct('OriginalFilename', '完美世界扫号工具.exe'),
         StringStruct('ProductName', '完美世界扫号工具'),
         StringStruct('ProductVersion', '{VSTR}')])
    ]),
    VarFileInfo([VarStruct('Translation', [2052, 1200])])
  ]
)
'''

out = os.path.join(HERE, "version_info.txt")
io.open(out, "w", encoding="utf-8").write(CONTENT)
print("已生成", out)
print("版本", VSTR)
