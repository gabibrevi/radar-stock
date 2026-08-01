"""Traducción de etiquetas XBRL a métricas canónicas.

Cada empresa etiqueta sus cuentas con cierta libertad dentro de US-GAAP: unas
declaran los ingresos como `Revenues`, otras como
`RevenueFromContractWithCustomerExcludingAssessedTax`, y algunas usan varias a lo
largo de los años. Este módulo define, para cada métrica que el radar necesita,
la lista ordenada de etiquetas aceptables. El orden importa: se toma la primera
que la empresa haya reportado en ese periodo.

Ignorar esto es el error clásico al trabajar con XBRL y produce silenciosamente
miles de empresas con "ingresos cero".
"""

from __future__ import annotations

# qtrs en los datasets de la SEC: 0 = saldo puntual (balance),
# 1 = un trimestre, 4 = un año.
FLOW = "flow"  # magnitudes de periodo (cuenta de resultados, flujos de caja)
STOCK = "stock"  # saldos a fecha (balance)

CONCEPTS: dict[str, tuple[str, tuple[str, ...]]] = {
    # ---------------- Cuenta de resultados ----------------
    "revenue": (
        FLOW,
        (
            "RevenueFromContractWithCustomerExcludingAssessedTax",
            "RevenueFromContractWithCustomerIncludingAssessedTax",
            "Revenues",
            "SalesRevenueNet",
            "SalesRevenueGoodsNet",
            "SalesRevenueServicesNet",
            "RevenuesNetOfInterestExpense",
        ),
    ),
    "cost_of_revenue": (
        FLOW,
        (
            "CostOfRevenue",
            "CostOfGoodsAndServicesSold",
            "CostOfGoodsSold",
            "CostOfServices",
        ),
    ),
    "gross_profit": (FLOW, ("GrossProfit",)),
    "rd_expense": (FLOW, ("ResearchAndDevelopmentExpense",)),
    "sga_expense": (
        FLOW,
        (
            "SellingGeneralAndAdministrativeExpense",
            "GeneralAndAdministrativeExpense",
        ),
    ),
    "operating_income": (
        FLOW,
        (
            "OperatingIncomeLoss",
            "IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest",
        ),
    ),
    "pretax_income": (
        FLOW,
        (
            "IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest",
            "IncomeLossFromContinuingOperationsBeforeIncomeTaxesMinorityInterestAndIncomeLossFromEquityMethodInvestments",
        ),
    ),
    "income_tax": (FLOW, ("IncomeTaxExpenseBenefit",)),
    "net_income": (FLOW, ("NetIncomeLoss", "ProfitLoss")),
    "interest_expense": (
        FLOW,
        ("InterestExpense", "InterestExpenseDebt", "InterestIncomeExpenseNet"),
    ),
    "eps_diluted": (
        FLOW,
        ("EarningsPerShareDiluted", "EarningsPerShareBasicAndDiluted"),
    ),
    "eps_basic": (FLOW, ("EarningsPerShareBasic", "EarningsPerShareBasicAndDiluted")),
    "shares_diluted": (
        FLOW,
        (
            "WeightedAverageNumberOfDilutedSharesOutstanding",
            "WeightedAverageNumberOfShareOutstandingBasicAndDiluted",
        ),
    ),
    # ---------------- Flujos de caja ----------------
    "operating_cash_flow": (
        FLOW,
        (
            "NetCashProvidedByUsedInOperatingActivities",
            "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations",
        ),
    ),
    "capex": (
        FLOW,
        (
            "PaymentsToAcquirePropertyPlantAndEquipment",
            "PaymentsToAcquireProductiveAssets",
            "PaymentsForCapitalImprovements",
        ),
    ),
    "depreciation_amortization": (
        FLOW,
        (
            "DepreciationDepletionAndAmortization",
            "DepreciationAmortizationAndAccretionNet",
            "DepreciationAndAmortization",
            "Depreciation",
        ),
    ),
    "stock_comp": (FLOW, ("ShareBasedCompensation",)),
    "buybacks": (FLOW, ("PaymentsForRepurchaseOfCommonStock",)),
    "dividends_paid": (
        FLOW,
        ("PaymentsOfDividendsCommonStock", "PaymentsOfDividends"),
    ),
    "equity_issued": (
        FLOW,
        (
            "ProceedsFromIssuanceOfCommonStock",
            "ProceedsFromIssuanceOrSaleOfEquity",
        ),
    ),
    # ---------------- Balance ----------------
    "assets": (STOCK, ("Assets",)),
    "liabilities": (STOCK, ("Liabilities",)),
    "equity": (
        STOCK,
        (
            "StockholdersEquity",
            "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest",
        ),
    ),
    "cash": (
        STOCK,
        (
            "CashAndCashEquivalentsAtCarryingValue",
            "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents",
        ),
    ),
    "short_term_investments": (
        STOCK,
        (
            "ShortTermInvestments",
            "MarketableSecuritiesCurrent",
            "AvailableForSaleSecuritiesDebtSecuritiesCurrent",
        ),
    ),
    "current_assets": (STOCK, ("AssetsCurrent",)),
    "current_liabilities": (STOCK, ("LiabilitiesCurrent",)),
    "inventory": (STOCK, ("InventoryNet",)),
    "receivables": (
        STOCK,
        ("AccountsReceivableNetCurrent", "ReceivablesNetCurrent"),
    ),
    "long_term_debt": (
        STOCK,
        ("LongTermDebtNoncurrent", "LongTermDebt", "LongTermDebtAndCapitalLeaseObligations"),
    ),
    "short_term_debt": (
        STOCK,
        (
            "LongTermDebtCurrent",
            "DebtCurrent",
            "ShortTermBorrowings",
            "OtherShortTermBorrowings",
        ),
    ),
    "goodwill": (STOCK, ("Goodwill",)),
    "ppe_net": (STOCK, ("PropertyPlantAndEquipmentNet",)),
    "shares_outstanding": (
        STOCK,
        (
            "CommonStockSharesOutstanding",
            "CommonStockSharesIssued",
            "EntityCommonStockSharesOutstanding",
        ),
    ),
}


def all_tags() -> set[str]:
    """Todas las etiquetas XBRL que nos interesan, para filtrar en la ingesta."""
    tags: set[str] = set()
    for _, candidates in CONCEPTS.values():
        tags.update(candidates)
    return tags


def tag_to_metrics() -> dict[str, list[str]]:
    """Índice inverso: etiqueta XBRL -> métricas canónicas que la aceptan."""
    mapping: dict[str, list[str]] = {}
    for metric, (_, candidates) in CONCEPTS.items():
        for tag in candidates:
            mapping.setdefault(tag, []).append(metric)
    return mapping


def tag_priority() -> dict[tuple[str, str], int]:
    """Prioridad de cada (métrica, etiqueta). Menor es mejor."""
    return {
        (metric, tag): i
        for metric, (_, candidates) in CONCEPTS.items()
        for i, tag in enumerate(candidates)
    }


def metric_kind(metric: str) -> str:
    return CONCEPTS[metric][0]


FLOW_METRICS = [m for m, (kind, _) in CONCEPTS.items() if kind == FLOW]
STOCK_METRICS = [m for m, (kind, _) in CONCEPTS.items() if kind == STOCK]
