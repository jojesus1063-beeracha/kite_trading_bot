import json
from pathlib import Path

import pandas as pd

INPUT = Path(
    "runtime/"
    "pnl_by_rvol_all_trades_20260815.csv"
)

OUTPUT_JSON = Path(
    "runtime/"
    "replay_rvol_070_100_reverse_above_200_20260815.json"
)

OUTPUT_CSV = Path(
    "runtime/"
    "replay_rvol_070_100_reverse_above_200_20260815.csv"
)


def reverse_direction(direction):
    direction = str(
        direction or ""
    ).strip().upper()

    if direction in {"BUY", "LONG"}:
        return "SELL"

    if direction in {"SELL", "SHORT"}:
        return "BUY"

    return "UNKNOWN"


def number(value):
    value = pd.to_numeric(
        value,
        errors="coerce",
    )

    return (
        float(value)
        if pd.notna(value)
        else None
    )


def summarise(frame):
    trades = len(frame)
    wins = int(
        (frame["counterfactual_net_pnl"] > 0)
        .sum()
    )
    losses = int(
        (frame["counterfactual_net_pnl"] < 0)
        .sum()
    )
    flats = trades - wins - losses

    return {
        "trades": trades,
        "wins": wins,
        "losses": losses,
        "flats": flats,
        "win_rate": (
            wins / trades * 100
            if trades
            else 0
        ),
        "gross_pnl": float(
            frame[
                "counterfactual_gross_pnl"
            ].sum()
        ),
        "costs": float(
            frame[
                "counterfactual_costs"
            ].sum()
        ),
        "net_pnl": float(
            frame[
                "counterfactual_net_pnl"
            ].sum()
        ),
        "average_net_per_trade": (
            float(
                frame[
                    "counterfactual_net_pnl"
                ].mean()
            )
            if trades
            else 0
        ),
    }


def main():
    if not INPUT.exists():
        raise SystemExit(
            f"INPUT_MISSING={INPUT}"
        )

    source = pd.read_csv(INPUT)

    required = {
        "date",
        "timestamp",
        "symbol",
        "direction",
        "gross_pnl",
        "costs",
        "net_pnl",
        "volume_ratio",
    }

    missing = required - set(source.columns)

    if missing:
        raise SystemExit(
            f"MISSING_COLUMNS={sorted(missing)}"
        )

    numeric_columns = [
        "gross_pnl",
        "costs",
        "net_pnl",
        "volume_ratio",
    ]

    for column in numeric_columns:
        source[column] = pd.to_numeric(
            source[column],
            errors="coerce",
        )

    rows = []
    rejected = []
    unavailable_reverse = []

    for _, trade in source.iterrows():
        rvol = number(
            trade["volume_ratio"]
        )

        if rvol is None:
            rejected.append(
                {
                    "reason":
                        "RVOL_UNAVAILABLE",
                    "symbol":
                        trade["symbol"],
                    "date":
                        trade["date"],
                }
            )
            continue

        if 0.70 <= rvol < 1.00:
            policy = (
                "NORMAL_DIRECTION_"
                "RVOL_0.70_TO_1.00"
            )
            new_direction = str(
                trade["direction"]
            ).upper()

            gross = number(
                trade["gross_pnl"]
            )
            costs = number(
                trade["costs"]
            )
            net = number(
                trade["net_pnl"]
            )

            if gross is None:
                if net is not None and costs is not None:
                    gross = net + costs
                else:
                    gross = net or 0

            if costs is None:
                if gross is not None and net is not None:
                    costs = gross - net
                else:
                    costs = 0

            if net is None:
                net = gross - costs

            counterfactual_gross = gross
            counterfactual_costs = costs
            counterfactual_net = net

        elif rvol >= 2.00:
            policy = (
                "REVERSED_DIRECTION_"
                "RVOL_AT_OR_ABOVE_2.00"
            )
            new_direction = reverse_direction(
                trade["direction"]
            )

            original_gross = number(
                trade["gross_pnl"]
            )
            original_costs = number(
                trade["costs"]
            )
            original_net = number(
                trade["net_pnl"]
            )

            if original_gross is None:
                if (
                    original_net is not None
                    and original_costs is not None
                ):
                    original_gross = (
                        original_net
                        + original_costs
                    )

            if original_costs is None:
                if (
                    original_gross is not None
                    and original_net is not None
                ):
                    original_costs = (
                        original_gross
                        - original_net
                    )

            if (
                original_gross is None
                or original_costs is None
            ):
                unavailable_reverse.append(
                    {
                        "date":
                            trade["date"],
                        "symbol":
                            trade["symbol"],
                        "volume_ratio": rvol,
                        "reason":
                            "GROSS_OR_COSTS_UNAVAILABLE",
                    }
                )
                continue

            # Same entry/exit prices and holding
            # period, with the trade side reversed.
            counterfactual_gross = (
                -original_gross
            )
            counterfactual_costs = (
                max(original_costs, 0)
            )
            counterfactual_net = (
                counterfactual_gross
                - counterfactual_costs
            )

        else:
            rejected.append(
                {
                    "reason":
                        "RVOL_OUTSIDE_POLICY",
                    "symbol":
                        trade["symbol"],
                    "date":
                        trade["date"],
                    "volume_ratio": rvol,
                }
            )
            continue

        original_direction = str(
            trade["direction"]
        ).upper()

        original_net = number(
            trade["net_pnl"]
        ) or 0

        rows.append(
            {
                "date": str(
                    trade["date"]
                ),
                "timestamp": str(
                    trade["timestamp"]
                ),
                "symbol": str(
                    trade["symbol"]
                ),
                "original_direction":
                    original_direction,
                "counterfactual_direction":
                    new_direction,
                "volume_ratio": rvol,
                "policy": policy,
                "original_net_pnl":
                    original_net,
                "counterfactual_gross_pnl":
                    counterfactual_gross,
                "counterfactual_costs":
                    counterfactual_costs,
                "counterfactual_net_pnl":
                    counterfactual_net,
                "pnl_improvement": (
                    counterfactual_net
                    - original_net
                ),
                "exit_reason": str(
                    trade.get(
                        "exit_reason",
                        "",
                    )
                ),
            }
        )

    result = pd.DataFrame(rows)

    if result.empty:
        raise SystemExit(
            "NO_TRADES_PASSED_POLICY"
        )

    result["date"] = result[
        "date"
    ].astype(str)

    overall = summarise(result)

    by_policy = {}

    for policy, frame in result.groupby(
        "policy"
    ):
        by_policy[policy] = summarise(
            frame
        )

    by_date = {}

    for trade_date, frame in result.groupby(
        "date"
    ):
        by_date[str(trade_date)] = {
            "overall": summarise(frame),
            "policies": {
                policy: summarise(
                    policy_frame
                )
                for policy, policy_frame
                in frame.groupby("policy")
            },
        }

    original_selected_net = float(
        result["original_net_pnl"].sum()
    )

    report = {
        "read_only": True,
        "cohort":
            "ACTUAL_RECORDED_TRADES_WITH_RVOL",
        "method":
            "SAME_ENTRY_AND_EXIT_TIMESTAMP_"
            "DIRECTIONAL_COUNTERFACTUAL",
        "policy": {
            "normal_direction":
                "0.70 <= RVOL < 1.00",
            "reversed_direction":
                "RVOL >= 2.00",
            "all_other_rvol":
                "REJECT",
        },
        "important_limitations": [
            "This is not a candle-by-candle "
            "stop/target replay.",
            "Reversed trades use the original "
            "entry and exit timestamps.",
            "Original costs are charged to the "
            "reversed trade.",
            "The policy was selected after "
            "examining this same dataset.",
        ],
        "source_trade_count": len(source),
        "admitted_trade_count": len(result),
        "rejected_trade_count":
            len(rejected),
        "reverse_unavailable_count":
            len(unavailable_reverse),
        "original_selected_net_pnl":
            original_selected_net,
        "counterfactual_summary":
            overall,
        "net_pnl_improvement": (
            overall["net_pnl"]
            - original_selected_net
        ),
        "by_policy": by_policy,
        "by_date": by_date,
        "reverse_unavailable":
            unavailable_reverse,
        "trades": result.to_dict(
            orient="records"
        ),
    }

    OUTPUT_JSON.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    OUTPUT_JSON.write_text(
        json.dumps(
            report,
            indent=2,
            default=str,
        ),
        encoding="utf-8",
    )

    result.to_csv(
        OUTPUT_CSV,
        index=False,
    )

    print("READ_ONLY_REPLAY=True")
    print(
        "POLICY_NORMAL=0.70<=RVOL<1.00"
    )
    print(
        "POLICY_REVERSE=RVOL>=2.00"
    )
    print(
        "OTHER_RVOL=REJECT"
    )
    print(
        "ENTRY_EXIT_TIMES=UNCHANGED"
    )
    print(
        "LIVE_STRATEGY_CHANGED=False"
    )

    print("\nOVERALL")
    print(
        f"Source trades       : "
        f"{len(source)}"
    )
    print(
        f"Admitted trades     : "
        f"{len(result)}"
    )
    print(
        f"Rejected trades     : "
        f"{len(rejected)}"
    )
    print(
        f"Reverse unavailable : "
        f"{len(unavailable_reverse)}"
    )
    print(
        f"Wins / losses       : "
        f"{overall['wins']} / "
        f"{overall['losses']}"
    )
    print(
        f"Win rate            : "
        f"{overall['win_rate']:.2f}%"
    )
    print(
        f"Gross P&L           : "
        f"Rs {overall['gross_pnl']:+.2f}"
    )
    print(
        f"Costs               : "
        f"Rs {overall['costs']:.2f}"
    )
    print(
        f"Counterfactual net  : "
        f"Rs {overall['net_pnl']:+.2f}"
    )
    print(
        f"Original subset net : "
        f"Rs {original_selected_net:+.2f}"
    )
    print(
        f"Improvement         : "
        f"Rs "
        f"{report['net_pnl_improvement']:+.2f}"
    )

    print("\nBY POLICY")

    for policy, values in by_policy.items():
        print(
            f"{policy:<46} "
            f"trades={values['trades']:3d} "
            f"wins={values['wins']:3d} "
            f"losses={values['losses']:3d} "
            f"win_rate="
            f"{values['win_rate']:6.2f}% "
            f"net=Rs "
            f"{values['net_pnl']:+.2f}"
        )

    print("\nBY DATE")

    for trade_date in sorted(by_date):
        values = by_date[
            trade_date
        ]["overall"]

        print(
            f"{trade_date} "
            f"trades={values['trades']:3d} "
            f"wins={values['wins']:3d} "
            f"losses={values['losses']:3d} "
            f"win_rate="
            f"{values['win_rate']:6.2f}% "
            f"net=Rs "
            f"{values['net_pnl']:+.2f}"
        )

    print("\nINDIVIDUAL TRADES")

    columns = [
        "date",
        "timestamp",
        "symbol",
        "original_direction",
        "counterfactual_direction",
        "volume_ratio",
        "policy",
        "original_net_pnl",
        "counterfactual_net_pnl",
        "pnl_improvement",
    ]

    print(
        result[columns].to_string(
            index=False
        )
    )

    print(f"\nDETAIL={OUTPUT_JSON}")
    print(f"CSV={OUTPUT_CSV}")


if __name__ == "__main__":
    main()
