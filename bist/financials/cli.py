import argparse
import logging

from bist.companies.loader import (
    load_companies,
    resolve_price_symbols,
    resolve_symbols,
)
from bist.financials.services.download_statements import download
from bist.financials.services.earnings_per_share import (
    import_eps_postgres,
    refresh_eps_postgres,
    update_eps_postgres,
)
from bist.financials.services.postgres_financial_pipeline import (
    apply_medallion_migration,
    calculate_gold_financials_postgres,
    update_database_postgres,
)
from bist.financials.services.postgres_price_pipeline import (
    adjust_prices_postgres,
    update_prices_postgres,
)
from bist.financials.services.postgres_reference_data import (
    update_reference_data_postgres,
)
from bist.financials.services.postgres_setup import initialize_postgres
from bist.financials.services.price_earnings import calculate_price_earnings_postgres
from bist.financials.services.price_level import (
    format_price_level,
    parse_price_level,
    price_level_status,
    set_price_level,
)
from bist.financials.services.repair_cash_flows import repair_cash_flows_postgres
from bist.financials.services.repair_financials import repair_financials_postgres
from bist.logging_config import configure_logging

logger = logging.getLogger(__name__)


def positive_int(value):
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return parsed


def add_parsing_arguments(parser):
    parser.add_argument(
        "--parse-workers", type=positive_int, default=2,
        help="Local statement parsing processes (default: 2); use 1 for serial parsing. Database writes stay serial.",
    )


def add_company_arguments(parser):
    parser.add_argument(
        "--symbols",
        nargs="+",
        help="Company symbols to process. Defaults to all companies in PostgreSQL metadata.",
    )


def add_period_arguments(parser):
    parser.add_argument("--begin-year", type=int, required=True)
    parser.add_argument("--begin-quarter", type=int, choices=[1, 2, 3, 4], required=True)
    parser.add_argument("--end-year", type=int, required=True)
    parser.add_argument("--end-quarter", type=int, choices=[1, 2, 3, 4], required=True)


def price_level_argument(value):
    try:
        return parse_price_level(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from None


def build_parser():
    parser = argparse.ArgumentParser(description="Download and process BIST financial statements.")
    commands = parser.add_subparsers(dest="command", required=True)

    run_parser = commands.add_parser(
        "run", aliases=["run-postgres"],
        help="Refresh sources, download statements, and publish PostgreSQL financial data."
    )
    add_company_arguments(run_parser)
    add_period_arguments(run_parser)
    add_parsing_arguments(run_parser)
    run_parser.add_argument("--skip-download", action="store_true")
    run_parser.add_argument("--skip-reference-data", action="store_true")
    run_parser.add_argument("--request-sleep", type=float, default=20.0)
    run_parser.add_argument("--rate-limit-delay", type=float, default=300.0)

    download_parser = commands.add_parser("download", help="Download annual KAP statement archives only.")
    add_company_arguments(download_parser)
    download_parser.add_argument("--begin-year", type=int, required=True)
    download_parser.add_argument("--end-year", type=int, required=True)
    download_parser.add_argument(
        "--start-after", help="Resume after this company, excluding it; default company order is alphabetical."
    )
    download_parser.add_argument("--request-sleep", type=float, default=20.0)
    download_parser.add_argument("--rate-limit-delay", type=float, default=300.0)

    commands.add_parser(
        "update-reference-data", aliases=["update-reference-data-postgres"],
        help="Refresh PostgreSQL CPI and USD/TRY reference data only."
    )

    database_parser = commands.add_parser(
        "update-database", aliases=["update-database-postgres"],
        help="Parse local archives and rebuild PostgreSQL financial values only."
    )
    add_company_arguments(database_parser)
    add_period_arguments(database_parser)
    add_parsing_arguments(database_parser)

    gold_parser = commands.add_parser(
        "calculate-gold",
        help="Rebuild PostgreSQL Gold financial values from existing Silver data only.",
    )
    add_company_arguments(gold_parser)
    pe_parser = commands.add_parser(
        "calculate-pe", help="Calculate quarter-end P/E from published EPS and stored original prices only."
    )
    add_company_arguments(pe_parser)

    level_parser = commands.add_parser(
        "set-price-level",
        help="Change the project-wide CPI target month and recalculate every inflation-adjusted value.",
    )
    level_parser.add_argument(
        "price_level", type=price_level_argument, metavar="YYYY-MM",
        help="Target month with CPI data, for example 2026-10.",
    )
    commands.add_parser(
        "show-price-level",
        help="Show the project-wide CPI target month, the latest CPI month and published consistency.",
    )

    prices_parser = commands.add_parser(
        "update-prices", aliases=["update-prices-postgres"],
        help="Download historical prices into PostgreSQL and adjust them to the project price level."
    )
    add_company_arguments(prices_parser)
    prices_parser.add_argument("--begin-date", required=True, help="YYYY-MM-DD or DD-MM-YYYY")
    prices_parser.add_argument("--end-date", required=True, help="YYYY-MM-DD or DD-MM-YYYY")
    prices_parser.add_argument("--request-sleep", type=float, default=1.5)
    prices_parser.add_argument("--rate-limit-delay", type=float, default=120.0)

    setup_parser = commands.add_parser(
        "init-postgres", help="Initialize the PostgreSQL schema and seed empty company metadata."
    )
    setup_parser.add_argument(
        "--create-database", action="store_true",
        help="Create bist_finance if needed, using the same connection settings against postgres.",
    )
    commands.add_parser("migrate-postgres", help="Create the PostgreSQL Bronze/Silver/Gold/Ops schema.")
    repair_parser = commands.add_parser(
        "repair-cash-flows", help="Restore cash-flow periods from Bronze and refresh published Gold data."
    )
    add_company_arguments(repair_parser)
    financial_repair_parser = commands.add_parser('repair-financials', help='Restore reported quarter/YTD facts and report price bases from retained sources.')
    financial_repair_parser.add_argument('--symbols', nargs='+', help='Defaults to all companies with active Silver filings.')
    eps_parser = commands.add_parser("update-eps", help="Extract reported EPS from local PDFs and refresh published Gold.")
    eps_parser.add_argument("--symbols", nargs="+", help="Defaults to companies with published Gold data.")
    eps_parser.add_argument("--workers",type=positive_int,default=2,help="Local EPS/PDF extraction workers (default: 2).")
    eps_parser.add_argument('--ocr', action='store_true', help='OCR image-only PDF pages locally; requires pdftoppm and tesseract.')
    eps_calculate_parser = commands.add_parser('calculate-eps', help='Refresh EPS, coverage estimates and P/E using stored sources without re-reading statements.')
    eps_calculate_parser.add_argument('--symbols', nargs='+', help='Defaults to companies with published Gold data.')
    eps_import_parser = commands.add_parser("import-eps", help="Import verified period-specific EPS inputs from JSON.")
    eps_import_parser.add_argument("--file", required=True)
    return parser


def resolve_companies(symbols):
    companies_by_symbol = load_companies()
    if not symbols:
        return list(companies_by_symbol.values())

    normalized_symbols = resolve_symbols(symbols)
    return [companies_by_symbol[symbol] for symbol in normalized_symbols]


def download_statements(companies, begin_year, end_year, request_sleep, rate_limit_delay):
    logger.info("Downloading archives for %s companies, %s-%s", len(companies), begin_year, end_year)
    download(
        begin_year=begin_year,
        end_year=end_year,
        companies=companies,
        request_sleep=request_sleep,
        rate_limit_delay=rate_limit_delay,
    )


def process_database_postgres(companies, args):
    logger.info(
        "Updating database for %s companies, %s Q%s through %s Q%s",
        len(companies), args.begin_year, args.begin_quarter, args.end_year, args.end_quarter,
    )
    ingested, run_id = update_database_postgres(
        companies, args.begin_year, args.begin_quarter,
        args.end_year, args.end_quarter,
        parse_workers=args.parse_workers,
    )
    logger.info("PostgreSQL processed %s files; Gold calculation run %s", ingested, run_id)


def print_price_level_status(status):
    stored, latest = status["stored"], status["latest_cpi"]
    print(f"Project price level: {format_price_level(stored)}")
    print(f"Latest CPI month:    {format_price_level(latest)}")
    published = ", ".join(format_price_level(level) for level in status["published"]) or "none"
    print(f"Published months:    {published}")
    if stored is None and len(status["published"]) > 1:
        print("Published data is mixed. Run set-price-level YYYY-MM to recalculate it at one month.")
    elif stored is None:
        print("Not set yet: the next Gold or price run adopts the published month, or the latest CPI month.")
    elif any(status["mismatches"].values()):
        print(f"WARNING: rows not at the project price level: {status['mismatches']}")
    else:
        print("All published inflation-adjusted values use the project price level.")
    if stored and latest and latest > stored:
        print(f"Newer CPI is available. Rebase with: python -m bist.financials set-price-level "
              f"{format_price_level(latest)}")
    for row in status["history"]:
        previous = (row["previous_year"], row["previous_month"]) if row["previous_year"] else None
        print(f"  {row['changed_at']:%Y-%m-%d %H:%M} {format_price_level(previous)} -> "
              f"{format_price_level((row['target_year'], row['target_month']))} "
              f"(run {row['run_id']}, {row['details'].get('source')})")


def main(argv=None):
    configure_logging()
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command in {"run", "run-postgres", "update-database", "update-database-postgres"}:
        begin_period = args.begin_year * 4 + args.begin_quarter
        end_period = args.end_year * 4 + args.end_quarter
        if begin_period > end_period:
            parser.error("the begin year/quarter must not be later than the end year/quarter")
    elif args.command == "download" and args.begin_year > args.end_year:
        parser.error("--begin-year must not be later than --end-year")

    if args.command in {"update-reference-data", "update-reference-data-postgres"}:
        update_reference_data_postgres()
        return
    if args.command == "migrate-postgres":
        apply_medallion_migration()
        return
    if args.command == "set-price-level":
        result = set_price_level(*args.price_level)
        print(
            f"Project price level: {format_price_level(result['price_level'])} "
            f"(was {format_price_level(result['previous'])}). Recalculated "
            f"{result['companies']} companies and all price series under run {result['run_id']}."
        )
        return
    if args.command == "show-price-level":
        print_price_level_status(price_level_status())
        return
    if args.command == "init-postgres":
        initialize_postgres(create_database=args.create_database)
        return
    if args.command == "repair-cash-flows":
        companies = resolve_companies(args.symbols) if args.symbols else None
        repair_cash_flows_postgres(companies)
        return
    if args.command == 'repair-financials':
        companies = resolve_companies(args.symbols) if args.symbols else None
        logger.info('Financial measurement repair completed: %s', repair_financials_postgres(companies))
        return
    if args.command == "update-eps":
        companies = resolve_companies(args.symbols) if args.symbols else None
        logger.info("EPS ingestion completed: %s", update_eps_postgres(companies,extraction_workers=args.workers,ocr=args.ocr))
        return
    if args.command == "import-eps":
        logger.info("EPS import completed: %s", import_eps_postgres(args.file))
        return
    if args.command == 'calculate-eps':
        companies = resolve_companies(args.symbols) if args.symbols else None
        logger.info('EPS-only refresh completed under run %s', refresh_eps_postgres(companies))
        return

    if args.command == "download":
        companies = resolve_companies(args.symbols)
        if args.start_after:
            try:
                resume_symbol = resolve_symbols([args.start_after])[0]
                index = next(i for i, company in enumerate(companies) if company.symbol == resume_symbol)
            except (ValueError, StopIteration):
                parser.error(f"--start-after company {args.start_after!r} is not in the selected company list")
            companies = companies[index+1:]
            if not companies:
                logger.info("No companies remain after %s", resume_symbol)
                return
            logger.info("Resuming after %s: %s companies, starting with %s",
                        resume_symbol, len(companies), companies[0].symbol)
        download_statements(
            companies, args.begin_year, args.end_year, args.request_sleep, args.rate_limit_delay
        )
    elif args.command in {"update-database", "update-database-postgres"}:
        process_database_postgres(resolve_companies(args.symbols), args)
    elif args.command == "calculate-gold":
        companies = resolve_companies(args.symbols) if args.symbols else None
        logger.info(
            "Calculating Gold from existing Silver data for %s at the project price level. "
            "No statements will be read or downloaded.",
            f"{len(companies)} companies" if companies else "all companies",
        )
        run_id = calculate_gold_financials_postgres(companies=companies)
        logger.info("Gold calculation completed under run %s", run_id)
    elif args.command == "calculate-pe":
        companies = resolve_companies(args.symbols) if args.symbols else None
        logger.info("P/E result: %s", calculate_price_earnings_postgres(companies))
    elif args.command in {"run", "run-postgres"}:
        companies = resolve_companies(args.symbols)
        apply_medallion_migration()
        if not args.skip_reference_data:
            update_reference_data_postgres()
        if not args.skip_download:
            download_statements(
                companies, args.begin_year, args.end_year, args.request_sleep, args.rate_limit_delay
            )
        process_database_postgres(companies, args)
    elif args.command in {"update-prices", "update-prices-postgres"}:
        companies = resolve_companies(args.symbols)
        symbols = (
            resolve_price_symbols(args.symbols)
            if args.symbols
            else [company.symbol for company in companies]
        )
        logger.info("Downloading historic prices for %s companies", len(symbols))
        update_prices_postgres(
            symbols, args.begin_date, args.end_date,
            sleep_seconds=args.request_sleep, rate_limit_delay=args.rate_limit_delay,
        )
        adjust_prices_postgres(symbols)


if __name__ == "__main__":
    main()
