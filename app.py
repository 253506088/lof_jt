"""
LOF 基金套利监控系统
作者: 财务套利专家
功能: 监控 LOF 基金的场外申购、场内卖出套利机会
"""

import streamlit as st
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
import warnings
import logging
import json
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
warnings.filterwarnings('ignore')

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)

# 尝试导入 akshare
try:
    import akshare as ak
    AKSHARE_AVAILABLE = True
    logger.info("✅ Akshare 模块加载成功")
except ImportError:
    AKSHARE_AVAILABLE = False
    logger.error("❌ Akshare 未安装")

# 缓存配置
CACHE_DIR = os.path.join(os.getcwd(), "lof_cache")
if not os.path.exists(CACHE_DIR):
    os.makedirs(CACHE_DIR, exist_ok=True)
    logger.info(f"📁 创建缓存目录: {CACHE_DIR}")


def load_nav_cache(cache_date):
    """加载指定日期的净值缓存"""
    cache_file = os.path.join(CACHE_DIR, f"nav_cache_{cache_date}.json")
    if os.path.exists(cache_file):
        try:
            with open(cache_file, 'r', encoding='utf-8') as f:
                cache_data = json.load(f)
            logger.info(f"✅ 加载缓存文件: {cache_file}，共 {len(cache_data)} 条数据")
            return cache_data
        except Exception as e:
            logger.warning(f"⚠️ 缓存文件读取失败: {str(e)}")
            return {}
    return {}


def save_nav_cache(cache_date, nav_dict):
    """保存净值缓存到文件"""
    cache_file = os.path.join(CACHE_DIR, f"nav_cache_{cache_date}.json")
    try:
        # 确保所有值都可以被JSON序列化（转换日期为字符串）
        serializable_dict = {}
        for code, data in nav_dict.items():
            serializable_dict[code] = {
                '基金代码': str(data['基金代码']),
                '基金净值': float(data['基金净值']),
                '净值日期': str(data['净值日期'])  # 确保日期是字符串
            }
        
        with open(cache_file, 'w', encoding='utf-8') as f:
            json.dump(serializable_dict, f, ensure_ascii=False, indent=2)
        logger.info(f"✅ 缓存已保存: {cache_file}，共 {len(serializable_dict)} 条数据")
    except Exception as e:
        logger.error(f"❌ 缓存保存失败: {str(e)}", exc_info=True)


def fetch_single_nav(fund_code, start_date, end_date):
    """查询单只基金的净值（用于多线程）"""
    try:
        df_nav = ak.fund_etf_fund_info_em(
            fund=fund_code,
            start_date=start_date,
            end_date=end_date
        )
        
        if df_nav is not None and len(df_nav) > 0:
            latest_nav = df_nav.iloc[-1]
            return {
                '基金代码': fund_code,
                '基金净值': latest_nav['单位净值'],
                '净值日期': latest_nav['净值日期'],
                'success': True
            }
        else:
            return {'基金代码': fund_code, 'success': False, 'error': '无净值数据'}
    except Exception as e:
        return {'基金代码': fund_code, 'success': False, 'error': str(e)}


def get_lof_data():
    """获取 LOF 基金实时数据"""
    if not AKSHARE_AVAILABLE:
        logger.error("❌ Akshare 模块未安装，无法获取数据")
        st.error("❌ Akshare 未安装，请先安装：`pip install akshare`")
        return None
    
    try:
        # ========== 步骤 1：获取LOF场内行情列表 ==========
        logger.info("🔍 [步骤1/3] 开始调用 Akshare API: fund_lof_spot_em() - 获取 LOF 场内行情")
        
        df_market = ak.fund_lof_spot_em()
        
        logger.info(f"📊 场内行情数据行数: {len(df_market)}")
        logger.info(f"📋 场内行情列名: {df_market.columns.tolist()}")
        logger.info(f"\n📄 前 3 条原始数据:\n{df_market.head(3).to_string()}")
        
        # 检查必需的列
        required_columns = ['代码', '名称', '最新价', '成交额']
        missing_columns = [col for col in required_columns if col not in df_market.columns]
        
        if missing_columns:
            error_msg = f"场内行情数据缺少必需列: {missing_columns}"
            logger.error(f"❌ {error_msg}")
            st.error(f"❌ {error_msg}")
            return None
        
        # 重命名列
        df_market = df_market.rename(columns={
            '代码': '基金代码',
            '名称': '基金名称',
            '最新价': '场内价格',
            '成交额': '场内成交额'
        })
        
        # 数据类型转换
        df_market['场内价格'] = pd.to_numeric(df_market['场内价格'], errors='coerce')
        df_market['场内成交额'] = pd.to_numeric(df_market['场内成交额'], errors='coerce')
        
        # 只保留需要的列
        df_market = df_market[['基金代码', '基金名称', '场内价格', '场内成交额']]
        logger.info(f"✅ 场内行情处理完成，共 {len(df_market)} 只 LOF")
        
        
        # ========== 步骤 2：从缓存或API获取净值数据 ==========
        cache_date = datetime.now().strftime("%Y%m%d")
        logger.info(f"🔍 [步骤2/3] 检查缓存: {cache_date}")
        
        # 加载缓存
        nav_cache = load_nav_cache(cache_date)
        
        # 确定哪些基金需要查询
        fund_codes = df_market['基金代码'].tolist()
        cached_codes = set(nav_cache.keys())
        need_fetch_codes = [code for code in fund_codes if code not in cached_codes]
        
        logger.info(f"📦 缓存命中: {len(cached_codes)} 只，需要查询: {len(need_fetch_codes)} 只")
        
        nav_data = []
        
        # 从缓存加载已有数据
        for code in fund_codes:
            if code in nav_cache:
                nav_data.append(nav_cache[code])
        
        # 如果有需要查询的基金，使用多线程查询
        if need_fetch_codes:
            st.info(f"🔄 需要查询 {len(need_fetch_codes)} 只基金的净值，使用3线程加速...")
            logger.info(f"🚀 开始多线程查询（3线程）...")
            
            end_date = datetime.now().strftime("%Y%m%d")
            start_date = (datetime.now() - timedelta(days=7)).strftime("%Y%m%d")
            
            success_count = 0
            fail_count = 0
            progress_bar = st.progress(0, text="正在获取基金净值...")
            
            # 使用线程池，3个线程并发
            with ThreadPoolExecutor(max_workers=3) as executor:
                # 提交所有任务
                future_to_code = {
                    executor.submit(fetch_single_nav, code, start_date, end_date): code
                    for code in need_fetch_codes
                }
                
                # 收集结果
                completed = 0
                for future in as_completed(future_to_code):
                    result = future.result()
                    completed += 1
                    
                    if result['success']:
                        # 添加到结果列表
                        nav_info = {
                            '基金代码': result['基金代码'],
                            '基金净值': result['基金净值'],
                            '净值日期': result['净值日期']
                        }
                        nav_data.append(nav_info)
                        # 更新缓存字典
                        nav_cache[result['基金代码']] = nav_info
                        success_count += 1
                    else:
                        logger.warning(f"⚠️ {result['基金代码']} 查询失败: {result.get('error', '未知错误')}")
                        fail_count += 1
                    
                    # 更新进度条
                    progress = completed / len(need_fetch_codes)
                    progress_bar.progress(progress, text=f"正在获取基金净值... ({completed}/{len(need_fetch_codes)})")
            
            progress_bar.empty()
            logger.info(f"✅ 新查询完成：成功 {success_count} 只，失败 {fail_count} 只")
            
            # 保存更新后的缓存
            if success_count > 0:
                save_nav_cache(cache_date, nav_cache)
        else:
            st.success("✅ 全部数据来自缓存，无需查询API")
            logger.info("✅ 全部数据来自缓存")
        
        if len(nav_data) == 0:
            st.error("❌ 无法获取任何基金的净值数据")
            return None
        
        # 转换为 DataFrame
        df_nav = pd.DataFrame(nav_data)
        df_nav['基金净值'] = pd.to_numeric(df_nav['基金净值'], errors='coerce')
        
        logger.info(f"📊 净值数据总数: {len(df_nav)} 条")
        logger.info(f"\n📊 净值数据前 5 条:\n{df_nav.head().to_string()}")
        
        
        # ========== 步骤 3：合并场内行情和净值数据 ==========
        logger.info("🔗 [步骤3/3] 合并场内行情和净值数据")
        
        df = pd.merge(df_market, df_nav, on='基金代码', how='inner')  # 内连接，只保留有净值的
        
        logger.info(f"📊 合并后数据行数: {len(df)}")
        logger.info(f"\n📄 合并后前 5 条:\n{df.head().to_string()}")
        
        # 添加辅助字段
        df['实时估值'] = df['基金净值']
        
        # 标记无效数据（而不是删除）
        df['数据状态'] = '正常'
        invalid_mask = (
            df['场内价格'].isna() | 
            df['基金净值'].isna() | 
            df['场内成交额'].isna() |
            (df['场内价格'] <= 0) |
            (df['基金净值'] <= 0)
        )
        df.loc[invalid_mask, '数据状态'] = '数据无效'
        
        invalid_count = invalid_mask.sum()
        if invalid_count > 0:
            logger.warning(f"⚠️ 发现无效数据: {invalid_count} 条（已标记，保留在全量表中）")
        
        result_df = df[['基金代码', '基金名称', '场内价格', '基金净值', '实时估值', '场内成交额', '数据状态']]
        
        valid_count = len(df) - invalid_count
        logger.info(f"✅ 数据处理完成，共 {len(result_df)} 条数据（有效: {valid_count}，无效: {invalid_count}）")
        logger.info(f"\n📊 最终数据前 5 条:\n{result_df.head().to_string()}")
        
        st.success(f"✅ 成功获取 {len(result_df)} 只 LOF 基金数据（有效: {valid_count}，无效: {invalid_count}）")
        
        return result_df
        
    except Exception as e:
        error_msg = f"获取数据失败: {str(e)}"
        logger.error(f"❌ {error_msg}", exc_info=True)
        st.error(f"❌ {error_msg}")
        st.error(f"异常类型: {type(e).__name__}")
        return None


def calculate_premium_rate(df):
    """计算溢价率"""
    df['溢价率(%)'] = ((df['场内价格'] - df['基金净值']) / df['基金净值'] * 100).round(2)
    return df


def filter_opportunities(df, min_premium, min_turnover):
    """筛选套利机会"""
    # 过滤条件（排除无效数据）
    filtered = df[
        (df['数据状态'] == '正常') &
        (df['溢价率(%)'] > min_premium) &
        (df['场内成交额'] > min_turnover)
    ].copy()
    
    return filtered


def highlight_premium_level(row):
    """根据溢价率高亮显示"""
    premium = row['溢价率(%)']
    
    if premium >= 5.0:
        # 高溢价：红色高亮（鸡腿机会）
        return ['background-color: #ffcccc; font-weight: bold; color: #d32f2f'] * len(row)
    elif premium >= 2.0:
        # 中等溢价：黄色高亮
        return ['background-color: #fff9c4; font-weight: bold; color: #f57c00'] * len(row)
    else:
        return [''] * len(row)


def highlight_with_invalid(row):
    """根据溢价率和数据状态高亮显示（用于全量表）"""
    # 检查是否为无效数据
    if '数据状态' in row.index and row['数据状态'] == '数据无效':
        # 无效数据：灰色背景
        return ['background-color: #e0e0e0; color: #757575; font-style: italic'] * len(row)
    
    premium = row['溢价率(%)']
    
    # 处理 NaN 的情况
    if pd.isna(premium):
        return ['background-color: #e0e0e0; color: #757575; font-style: italic'] * len(row)
    
    if premium >= 5.0:
        # 高溢价：红色高亮（鸡腿机会）
        return ['background-color: #ffcccc; font-weight: bold; color: #d32f2f'] * len(row)
    elif premium >= 2.0:
        # 中等溢价：黄色高亮
        return ['background-color: #fff9c4; font-weight: bold; color: #f57c00'] * len(row)
    else:
        return [''] * len(row)


def format_turnover(value):
    """格式化成交额显示"""
    if value >= 10000:
        return f"{value/10000:.2f} 万"
    else:
        return f"{value:.2f} 万"


def main():
    """主程序"""
    # 页面配置
    st.set_page_config(
        page_title="LOF 基金套利监控系统",
        page_icon="💰",
        layout="wide",
        initial_sidebar_state="expanded"
    )
    
    # 标题
    st.title("💰 LOF 基金套利监控系统")
    st.markdown("### 场外申购、场内卖出套利机会实时监控")
    st.markdown("---")
    
    # 侧边栏参数设置
    st.sidebar.header("📊 筛选参数设置")
    
    min_premium = st.sidebar.slider(
        "最小溢价率 (%)",
        min_value=0.0,
        max_value=10.0,
        value=1.5,
        step=0.1,
        help="只显示溢价率大于此值的基金"
    )
    
    min_turnover = st.sidebar.slider(
        "最小成交额 (万元)",
        min_value=0,
        max_value=500,
        value=50,
        step=10,
        help="过滤流动性较差的品种"
    )
    
    st.sidebar.markdown("---")
    st.sidebar.header("🛡️ 账户设置")
    is_free_five = st.sidebar.checkbox(
        "账户已免五",
        value=True,
        help="免五是指免除交易佣金最低 5 元的限制。如果未免五，每笔申购/卖出最低收取 5 元手续费。"
    )
    
    invest_amount = st.sidebar.number_input(
        "计划申购金额 (元)",
        min_value=100,
        max_value=1000000,
        value=100,
        step=100,
        help="用于计算扣除手续费后的实际利润"
    )
    
    st.sidebar.markdown("---")
    st.sidebar.markdown("### 💡 使用说明")
    st.sidebar.markdown("⚠️ **注意**：由于无法获取真实的申购状态和限额，所以移除了这些字段。🍗 鸡腿机会只根据溢价率判断。")
    st.sidebar.markdown("🍗 **什么是鸡腿机会**：我爱吃鸡腿，一般有套利机会的LOF基金一般都限购100，套利赚取的钱刚好加个鸡腿。如果你爱喝奶茶，那么可以叫奶茶机会")

    # 刷新按钮
    col1, col2 = st.columns([1, 5])
    with col1:
        if st.button("🔄 刷新数据", width="stretch"):
            # 清除缓存，强制重新获取
            if 'lof_data' in st.session_state:
                del st.session_state['lof_data']
            st.rerun()
    
    # 获取数据 (优先使用缓存)
    if 'lof_data' not in st.session_state:
        with st.spinner("正在获取 LOF 基金数据..."):
            df_raw = get_lof_data()
        if df_raw is not None and len(df_raw) > 0:
            st.session_state['lof_data'] = df_raw
    
    if 'lof_data' not in st.session_state or st.session_state['lof_data'] is None:
        st.error("❌ 无法获取数据，请检查网络连接或稍后重试")
        return
    
    # 使用缓存的原始数据进行计算
    df = st.session_state['lof_data'].copy()
    
    # 计算溢价率
    df = calculate_premium_rate(df)
    
    # 风险提示 (如果不免五)
    if not is_free_five:
        st.warning(f"⚠️ **风险提示**：您的账户**未免五**。系统已自动在套利计算中扣除 **5 元**最低手续费，请确保单笔申购金额 {invest_amount} 元能覆盖成本。")
    
    # 筛选机会（min_turnover 单位是万元，需要转换为元）
    filtered_df = filter_opportunities(df, min_premium, min_turnover * 10000)
    
    # 计算预估利润
    fee = 0 if is_free_five else 5
    profit_col_name = '预估利润' if is_free_five else '预估利润(扣5元)'
    # 添加申购金额列（放在预估利润前）
    filtered_df['申购金额'] = invest_amount
    df['申购金额'] = invest_amount
    filtered_df[profit_col_name] = (invest_amount * filtered_df['溢价率(%)'] / 100 - fee).round(2)
    df[profit_col_name] = (invest_amount * df['溢价率(%)'] / 100 - fee).round(2)
    
    # 按溢价率降序排序
    filtered_df = filtered_df.sort_values('溢价率(%)', ascending=False)
    
    # 显示统计信息
    st.markdown("### 📈 数据概览")
    col1, col2, col3, col4 = st.columns(4)
    
    with col1:
        st.metric("总LOF数量", len(df))
    
    with col2:
        st.metric("符合条件", len(filtered_df))
    
    with col3:
        # 统计鸡腿机会（溢价率 >= 5%）
        chicken_leg_count = len(filtered_df[filtered_df['溢价率(%)'] >= 5.0])
        st.metric("🍗 鸡腿机会", chicken_leg_count, delta="溢价≥5%")
    
    with col4:
        if len(filtered_df) > 0:
            max_premium = filtered_df['溢价率(%)'].max()
            st.metric("最高溢价率", f"{max_premium:.2f}%")
        else:
            st.metric("最高溢价率", "N/A")
    
    st.markdown("---")
    
    # 使用 Tab 分别显示筛选结果和全量数据
    tab1, tab2 = st.tabs(["📋 套利机会列表", "📊 全量LOF数据"])
    
    with tab1:
        # 显示筛选后的数据表格
        if len(filtered_df) > 0:
            st.markdown("🟥 **红色** = 高溢价(≥5%) | 🟡 **黄色** = 中等溢价(2-5%)")
            
            # 对数据应用溢价率分级高亮
            styled_df = filtered_df.style.apply(highlight_premium_level, axis=1)
            
            # 格式化特定列的显示
            format_dict = {'场内成交额': format_turnover, profit_col_name: "￥{:.2f}"}
            
            styled_df = styled_df.format(format_dict)
            
            # 显示表格
            st.dataframe(
                styled_df,
                width='stretch',
                height=600,
                hide_index=True
            )
            
            # 导出功能
            st.markdown("---")
            csv = filtered_df.to_csv(index=False, encoding='utf-8-sig')
            st.download_button(
                label="📥 导出筛选结果为 CSV",
                data=csv,
                file_name=f"LOF套利机会_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv",
                mime="text/csv"
            )
            
        else:
            st.warning("⚠️ 当前没有符合筛选条件的套利机会")
            st.info("💡 提示：尝试降低溢价率或成交额阈值")
    
    with tab2:
        # 统计无效数据数量
        invalid_count = len(df[df['数据状态'] == '数据无效'])
        valid_count = len(df) - invalid_count
        
        # 显示全量数据
        st.markdown(f"**全量数据** - 共 {len(df)} 只 LOF 基金（有效: {valid_count}，无效: {invalid_count}）")
        st.info("💡 此列表显示所有 LOF 基金，包括数据不完整的基金（灰色标记）")
        st.markdown("🟥 **红色** = 高溢价(≥5%) | 🟡 **黄色** = 中等溢价(2-5%) | ⬜ **灰色** = 数据无效（停牌/缺失）")
        
        # 对全量数据按溢价率排序（无效数据排在最后）
        df_sorted = df.sort_values(['数据状态', '溢价率(%)'], ascending=[True, False])
        
        # 应用高亮（使用支持无效数据标记的函数）
        styled_all_df = df_sorted.style.apply(highlight_with_invalid, axis=1)
        
        # 格式化显示
        format_dict_all = {'场内成交额': format_turnover, profit_col_name: "￥{:.2f}"}
        
        styled_all_df = styled_all_df.format(format_dict_all)
        
        # 显示全量表格
        st.dataframe(
            styled_all_df,
            width='stretch',
            height=600,
            hide_index=True
        )
        
        # 导出全量数据
        st.markdown("---")
        csv_all = df_sorted.to_csv(index=False, encoding='utf-8-sig')
        st.download_button(
            label="📥 导出全量数据为 CSV",
            data=csv_all,
            file_name=f"LOF全量数据_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv",
            mime="text/csv"
        )
    
    # 页脚
    st.markdown("---")
    st.markdown(
        """
        <div style='text-align: center; color: gray;'>
            <p>⚠️ 风险提示：套利有风险，投资需谨慎。本系统仅供参考，不构成投资建议。</p>
            <p>📊 数据更新时间：{}</p>
            <p>🔗 <a href="https://github.com/253506088/lof_jt" target="_blank">GitHub</a></p>
        </div>
        """.format(datetime.now().strftime('%Y-%m-%d %H:%M:%S')),
        unsafe_allow_html=True
    )


if __name__ == "__main__":
    main()
