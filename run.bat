@echo off
chcp 65001 >nul
echo ===================================
echo 用鼠标按住 run.bat，直接拖拽到【Anaconda Prompt】黑窗口里，然后按下回车键。
echo 正在准备启动 lof_jt 项目...
echo ===================================

:: 1. 切换盘符并进入项目目录
D:
cd D:\code\my\1\lof_jt

:: 2. 激活虚拟环境 (注意在bat里面使用conda需要加上call)
echo 正在激活 conda 环境 lof_jt...
call conda activate lof_jt

:: 3. 运行streamlit应用
echo 正在运行 app.py...
streamlit run app.py

:: 4. 避免闪退，停留查看报错
pause
