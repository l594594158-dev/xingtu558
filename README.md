# 星途558 - 山寨异动多空策略 v4.7

## 运行方式
crontab 每15分钟轮询，执行 `strategy_trade.py`

## 交易所
币安合约（Binance Futures）

## 配置
复制 `config/example.binance.json` 为 `config/binance.json`，
填入你的币安API密钥（需要合约交易权限）。

## 依赖安装
pip install -r requirements.txt

## 目录结构
├── strategy_trade.py          # 策略主程序
├── config/
│   └── example.binance.json   # API密钥模板
├── crontab.txt                # 轮询配置
├── requirements.txt           # 依赖
└── README.md                  # 本文件
