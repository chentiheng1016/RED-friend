"""Factory Supply Chain Management — 工廠供應鏈管理。

管理供應鏈全流程：
- 供應商績效評估 (Supplier Performance Evaluation)
- 採購訂單管理 (Purchase Order Management)
- 庫存優化 (Inventory Optimization)
- 物流協調 (Logistics Coordination)
- 供應鏈風險管理 (Supply Chain Risk Management)

集成ERP系統和供應商數據。
"""

import json
import os
from datetime import datetime, timedelta
from typing import Dict, List, Any, Optional
from dataclasses import dataclass
from statistics import mean

from agent_core.logging_and_paths import logger, _SCRIPT_DIR
from agent_core.gemini_client import _gemini_generate

# 供應鏈管理目錄
SUPPLY_CHAIN_DIR = os.path.join(_SCRIPT_DIR, "var", "factory_data", "supply_chain")
os.makedirs(SUPPLY_CHAIN_DIR, exist_ok=True)

@dataclass
class Supplier:
    """供應商信息"""
    id: str
    name: str
    category: str  # raw_materials, components, services
    contact_info: Dict[str, str]
    performance_score: float = 0.0
    contract_terms: Dict[str, Any] = None
    last_evaluation: Optional[datetime] = None

@dataclass
class PurchaseOrder:
    """採購訂單"""
    id: str
    supplier_id: str
    items: List[Dict[str, Any]]
    order_date: datetime
    expected_delivery: datetime
    actual_delivery: Optional[datetime] = None
    status: str = "pending"  # pending, confirmed, shipped, delivered, delayed
    total_value: float = 0.0

@dataclass
class InventoryItem:
    """庫存項目"""
    id: str
    name: str
    category: str
    current_stock: int
    reorder_point: int
    max_stock: int
    unit_cost: float
    supplier_id: str
    last_updated: datetime

class SupplyChainManager:
    """供應鏈管理器"""

    def __init__(self):
        self.suppliers = self._load_suppliers()
        self.purchase_orders = self._load_purchase_orders()
        self.inventory = self._load_inventory()

    def _load_suppliers(self) -> List[Supplier]:
        """載入供應商數據"""
        suppliers_file = os.path.join(SUPPLY_CHAIN_DIR, "suppliers.json")
        if os.path.exists(suppliers_file):
            try:
                with open(suppliers_file, 'r', encoding='utf-8') as f:
                    suppliers_data = json.load(f)
                    return [Supplier(**s) for s in suppliers_data]
            except Exception as e:
                logger.error(f"載入供應商數據失敗: {e}")

        # 默認供應商
        return [
            Supplier(
                id="SUP001",
                name="鋼材供應商A",
                category="raw_materials",
                contact_info={"email": "contact@supplierA.com", "phone": "+886-2-12345678"},
                performance_score=0.92
            ),
            Supplier(
                id="SUP002",
                name="塑膠供應商B",
                category="raw_materials",
                contact_info={"email": "contact@supplierB.com", "phone": "+886-2-87654321"},
                performance_score=0.87
            ),
            Supplier(
                id="SUP003",
                name="電子元件供應商C",
                category="components",
                contact_info={"email": "contact@supplierC.com", "phone": "+886-2-11223344"},
                performance_score=0.95
            )
        ]

    def _load_purchase_orders(self) -> List[PurchaseOrder]:
        """載入採購訂單"""
        orders_file = os.path.join(SUPPLY_CHAIN_DIR, "purchase_orders.json")
        if os.path.exists(orders_file):
            try:
                with open(orders_file, 'r', encoding='utf-8') as f:
                    orders_data = json.load(f)
                    return [PurchaseOrder(
                        id=o['id'],
                        supplier_id=o['supplier_id'],
                        items=o['items'],
                        order_date=datetime.fromisoformat(o['order_date']),
                        expected_delivery=datetime.fromisoformat(o['expected_delivery']),
                        actual_delivery=datetime.fromisoformat(o['actual_delivery']) if o.get('actual_delivery') else None,
                        status=o['status'],
                        total_value=o['total_value']
                    ) for o in orders_data]
            except Exception as e:
                logger.error(f"載入採購訂單失敗: {e}")

        # 生成模擬訂單
        return self._generate_mock_orders()

    def _generate_mock_orders(self) -> List[PurchaseOrder]:
        """生成模擬採購訂單"""
        orders = []
        base_date = datetime.now() - timedelta(days=30)

        for i in range(20):
            order_date = base_date + timedelta(days=i*1.5)
            expected_delivery = order_date + timedelta(days=7 + (i % 3))

            # 隨機決定是否延遲
            is_delayed = (i % 5) == 0
            actual_delivery = expected_delivery + timedelta(days=2) if is_delayed else expected_delivery

            supplier_id = f"SUP00{(i % 3) + 1}"

            items = [
                {
                    "name": f"材料{i+1}",
                    "quantity": 100 + i*10,
                    "unit_price": 50.0 + i*2,
                    "total": (100 + i*10) * (50.0 + i*2)
                }
            ]

            total_value = sum(item['total'] for item in items)

            orders.append(PurchaseOrder(
                id=f"PO{str(i+1).zfill(3)}",
                supplier_id=supplier_id,
                items=items,
                order_date=order_date,
                expected_delivery=expected_delivery,
                actual_delivery=actual_delivery if is_delayed else None,
                status="delivered" if not is_delayed else "delayed",
                total_value=total_value
            ))

        return orders

    def _load_inventory(self) -> List[InventoryItem]:
        """載入庫存數據"""
        inventory_file = os.path.join(SUPPLY_CHAIN_DIR, "inventory.json")
        if os.path.exists(inventory_file):
            try:
                with open(inventory_file, 'r', encoding='utf-8') as f:
                    inventory_data = json.load(f)
                    return [InventoryItem(
                        id=i['id'],
                        name=i['name'],
                        category=i['category'],
                        current_stock=i['current_stock'],
                        reorder_point=i['reorder_point'],
                        max_stock=i['max_stock'],
                        unit_cost=i['unit_cost'],
                        supplier_id=i['supplier_id'],
                        last_updated=datetime.fromisoformat(i['last_updated'])
                    ) for i in inventory_data]
            except Exception as e:
                logger.error(f"載入庫存數據失敗: {e}")

        # 默認庫存
        return [
            InventoryItem(
                id="INV001",
                name="鋼材A",
                category="raw_materials",
                current_stock=50000,
                reorder_point=10000,
                max_stock=100000,
                unit_cost=45.0,
                supplier_id="SUP001",
                last_updated=datetime.now()
            ),
            InventoryItem(
                id="INV002",
                name="塑膠B",
                category="raw_materials",
                current_stock=30000,
                reorder_point=8000,
                max_stock=60000,
                unit_cost=25.0,
                supplier_id="SUP002",
                last_updated=datetime.now()
            ),
            InventoryItem(
                id="INV003",
                name="電子元件C",
                category="components",
                current_stock=15000,
                reorder_point=3000,
                max_stock=30000,
                unit_cost=12.0,
                supplier_id="SUP003",
                last_updated=datetime.now()
            )
        ]

    def _save_suppliers(self):
        """保存供應商數據"""
        suppliers_file = os.path.join(SUPPLY_CHAIN_DIR, "suppliers.json")
        try:
            suppliers_data = [vars(s) for s in self.suppliers]
            with open(suppliers_file, 'w', encoding='utf-8') as f:
                json.dump(suppliers_data, f, ensure_ascii=False, indent=2, default=str)
        except Exception as e:
            logger.error(f"保存供應商數據失敗: {e}")

    def _save_purchase_orders(self):
        """保存採購訂單"""
        orders_file = os.path.join(SUPPLY_CHAIN_DIR, "purchase_orders.json")
        try:
            orders_data = [{
                "id": o.id,
                "supplier_id": o.supplier_id,
                "items": o.items,
                "order_date": o.order_date.isoformat(),
                "expected_delivery": o.expected_delivery.isoformat(),
                "actual_delivery": o.actual_delivery.isoformat() if o.actual_delivery else None,
                "status": o.status,
                "total_value": o.total_value
            } for o in self.purchase_orders]

            with open(orders_file, 'w', encoding='utf-8') as f:
                json.dump(orders_data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"保存採購訂單失敗: {e}")

    def _save_inventory(self):
        """保存庫存數據"""
        inventory_file = os.path.join(SUPPLY_CHAIN_DIR, "inventory.json")
        try:
            inventory_data = [{
                "id": i.id,
                "name": i.name,
                "category": i.category,
                "current_stock": i.current_stock,
                "reorder_point": i.reorder_point,
                "max_stock": i.max_stock,
                "unit_cost": i.unit_cost,
                "supplier_id": i.supplier_id,
                "last_updated": i.last_updated.isoformat()
            } for i in self.inventory]

            with open(inventory_file, 'w', encoding='utf-8') as f:
                json.dump(inventory_data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"保存庫存數據失敗: {e}")

    def evaluate_supplier_performance(self, supplier_id: str) -> Dict[str, Any]:
        """評估供應商績效"""
        supplier = next((s for s in self.suppliers if s.id == supplier_id), None)
        if not supplier:
            return {"error": "供應商不存在"}

        # 獲取該供應商的所有訂單
        supplier_orders = [o for o in self.purchase_orders if o.supplier_id == supplier_id]

        if not supplier_orders:
            return {
                "supplier_id": supplier_id,
                "performance_score": 0.0,
                "total_orders": 0,
                "on_time_delivery_rate": 0.0,
                "average_delay_days": 0
            }

        # 計算準時交付率
        delivered_orders = [o for o in supplier_orders if o.status in ["delivered", "delayed"]]
        on_time_deliveries = [o for o in delivered_orders
                            if o.actual_delivery and o.actual_delivery <= o.expected_delivery]

        on_time_rate = len(on_time_deliveries) / len(delivered_orders) if delivered_orders else 0

        # 計算平均延遲天數
        delays = []
        for order in delivered_orders:
            if order.actual_delivery and order.actual_delivery > order.expected_delivery:
                delay_days = (order.actual_delivery - order.expected_delivery).days
                delays.append(delay_days)

        avg_delay = mean(delays) if delays else 0

        # 綜合績效評分（權重：準時交付70%，延遲程度30%）
        delay_penalty = min(avg_delay / 10, 1.0)  # 延遲10天以上按最大懲罰
        performance_score = 0.7 * on_time_rate + 0.3 * (1 - delay_penalty)

        # 更新供應商績效
        supplier.performance_score = performance_score
        supplier.last_evaluation = datetime.now()
        self._save_suppliers()

        return {
            "supplier_id": supplier_id,
            "supplier_name": supplier.name,
            "performance_score": round(performance_score, 3),
            "total_orders": len(supplier_orders),
            "delivered_orders": len(delivered_orders),
            "on_time_delivery_rate": round(on_time_rate, 3),
            "average_delay_days": round(avg_delay, 1),
            "evaluation_date": datetime.now()
        }

    def create_purchase_order(self, supplier_id: str, items: List[Dict[str, Any]],
                            expected_delivery_days: int = 7) -> Optional[PurchaseOrder]:
        """創建採購訂單"""
        supplier = next((s for s in self.suppliers if s.id == supplier_id), None)
        if not supplier:
            logger.error(f"供應商不存在: {supplier_id}")
            return None

        # 生成訂單ID
        existing_ids = [int(o.id[2:]) for o in self.purchase_orders if o.id.startswith("PO")]
        next_id = max(existing_ids) + 1 if existing_ids else 1
        order_id = f"PO{str(next_id).zfill(3)}"

        # 計算總價值
        total_value = sum(item.get('quantity', 0) * item.get('unit_price', 0) for item in items)

        order = PurchaseOrder(
            id=order_id,
            supplier_id=supplier_id,
            items=items,
            order_date=datetime.now(),
            expected_delivery=datetime.now() + timedelta(days=expected_delivery_days),
            total_value=total_value
        )

        self.purchase_orders.append(order)
        self._save_purchase_orders()

        return order

    def update_order_status(self, order_id: str, status: str,
                          actual_delivery: Optional[datetime] = None) -> bool:
        """更新訂單狀態"""
        order = next((o for o in self.purchase_orders if o.id == order_id), None)
        if not order:
            logger.error(f"訂單不存在: {order_id}")
            return False

        order.status = status
        if actual_delivery:
            order.actual_delivery = actual_delivery

        self._save_purchase_orders()
        return True

    def check_inventory_levels(self) -> List[Dict[str, Any]]:
        """檢查庫存水平"""
        alerts = []

        for item in self.inventory:
            stock_ratio = item.current_stock / item.max_stock

            if item.current_stock <= item.reorder_point:
                alerts.append({
                    "item_id": item.id,
                    "item_name": item.name,
                    "alert_type": "reorder",
                    "current_stock": item.current_stock,
                    "reorder_point": item.reorder_point,
                    "message": f"{item.name} 庫存低於補貨點，需要補貨"
                })
            elif stock_ratio > 0.9:
                alerts.append({
                    "item_id": item.id,
                    "item_name": item.name,
                    "alert_type": "overstock",
                    "current_stock": item.current_stock,
                    "max_stock": item.max_stock,
                    "message": f"{item.name} 庫存過高，可能造成積壓"
                })

        return alerts

    def optimize_inventory(self) -> Dict[str, Any]:
        """庫存優化建議"""
        alerts = self.check_inventory_levels()

        # 分析庫存周轉率
        total_value = sum(item.current_stock * item.unit_cost for item in self.inventory)
        avg_stock_ratio = mean(item.current_stock / item.max_stock for item in self.inventory)

        recommendations = []

        if len([a for a in alerts if a['alert_type'] == 'reorder']) > 0:
            recommendations.append("優先處理低庫存項目補貨")

        if len([a for a in alerts if a['alert_type'] == 'overstock']) > 0:
            recommendations.append("評估過高庫存項目的銷售策略")

        if avg_stock_ratio < 0.3:
            recommendations.append("考慮增加安全庫存以減少缺貨風險")

        return {
            "total_inventory_value": round(total_value, 2),
            "average_stock_ratio": round(avg_stock_ratio, 3),
            "alerts_count": len(alerts),
            "recommendations": recommendations,
            "alerts": alerts
        }

    def get_supply_chain_risks(self) -> List[Dict[str, Any]]:
        """識別供應鏈風險"""
        risks = []

        # 供應商集中度風險
        supplier_usage = {}
        for order in self.purchase_orders[-100:]:  # 最近100筆訂單
            supplier_usage[order.supplier_id] = supplier_usage.get(order.supplier_id, 0) + order.total_value

        total_value = sum(supplier_usage.values())
        for supplier_id, value in supplier_usage.items():
            concentration = value / total_value
            if concentration > 0.5:  # 單一供應商佔比超過50%
                supplier = next((s for s in self.suppliers if s.id == supplier_id), None)
                if supplier:
                    risks.append({
                        "risk_type": "supplier_concentration",
                        "supplier_id": supplier_id,
                        "supplier_name": supplier.name,
                        "concentration": round(concentration, 3),
                        "severity": "high",
                        "recommendation": "分散供應來源以降低風險"
                    })

        # 延遲風險
        delayed_orders = [o for o in self.purchase_orders
                         if o.status == "delayed" and not o.actual_delivery]

        if len(delayed_orders) > 5:
            risks.append({
                "risk_type": "delivery_delays",
                "affected_orders": len(delayed_orders),
                "severity": "medium",
                "recommendation": "審查延遲訂單並尋找替代供應商"
            })

        # 庫存風險
        low_stock_items = [item for item in self.inventory if item.current_stock <= item.reorder_point]
        if len(low_stock_items) > 2:
            risks.append({
                "risk_type": "inventory_shortage",
                "affected_items": len(low_stock_items),
                "severity": "high",
                "recommendation": "緊急補貨關鍵材料"
            })

        return risks

    def generate_supply_chain_report(self) -> str:
        """生成供應鏈報告"""
        # 評估所有供應商績效
        supplier_performances = []
        for supplier in self.suppliers:
            perf = self.evaluate_supplier_performance(supplier.id)
            supplier_performances.append(perf)

        # 庫存優化
        inventory_status = self.optimize_inventory()

        # 風險評估
        risks = self.get_supply_chain_risks()

        # 訂單統計
        recent_orders = [o for o in self.purchase_orders
                        if o.order_date > datetime.now() - timedelta(days=30)]

        report = f"""供應鏈管理報告 - {datetime.now().strftime('%Y-%m-%d')}

📊 供應商績效：
• 總供應商數: {len(self.suppliers)}
• 平均績效評分: {mean([p.get('performance_score', 0) for p in supplier_performances]):.3f}
• 高績效供應商: {len([p for p in supplier_performances if p.get('performance_score', 0) > 0.9])}

📦 訂單統計：
• 最近30天訂單: {len(recent_orders)}
• 總價值: ${sum(o.total_value for o in recent_orders):,.2f}
• 延遲訂單: {len([o for o in recent_orders if o.status == 'delayed'])}

📈 庫存狀態：
• 總庫存價值: ${inventory_status['total_inventory_value']:,.2f}
• 平均庫存率: {inventory_status['average_stock_ratio']:.1%}
• 庫存警報: {inventory_status['alerts_count']} 項

⚠️ 供應鏈風險 ({len(risks)}):
"""

        for risk in risks[:5]:
            report += f"• {risk['risk_type']}: {risk['recommendation']}\n"

        report += "\n💡 改進建議：\n"
        for rec in inventory_status['recommendations'][:3]:
            report += f"• {rec}\n"

        return report

# 全域實例
supply_chain_manager = SupplyChainManager()

# 工具函數
def evaluate_supplier_performance(supplier_id: str):
    """評估供應商績效"""
    return supply_chain_manager.evaluate_supplier_performance(supplier_id)

def create_purchase_order(supplier_id: str, items: List[Dict[str, Any]], expected_delivery_days: int = 7):
    """創建採購訂單"""
    return supply_chain_manager.create_purchase_order(supplier_id, items, expected_delivery_days)

def check_inventory_alerts():
    """檢查庫存警報"""
    return supply_chain_manager.check_inventory_levels()

def get_supply_chain_risks():
    """獲取供應鏈風險"""
    return supply_chain_manager.get_supply_chain_risks()

def generate_supply_chain_report():
    """生成供應鏈報告"""
    return supply_chain_manager.generate_supply_chain_report()
