# """Batch persistence helpers for SQLAlchemy models."""

# from typing import Iterable
# from sqlalchemy.orm import Session

# from models.orm import OnchainTrade, PaperTrade, Signal, Trade, WalletProfile


# def batch_write_trades(db: Session, trades: Iterable[Trade]) -> None:
#     """Persist a batch of Trade records."""
#     # TODO: Add batching and error handling.
#     raise NotImplementedError("Batch trade write not implemented")


# def batch_write_onchain_trades(db: Session, trades: Iterable[OnchainTrade]) -> None:
#     """Persist a batch of OnchainTrade records."""
#     # TODO: Add batching and error handling.
#     raise NotImplementedError("Batch on-chain trade write not implemented")


# def batch_write_signals(db: Session, signals: Iterable[Signal]) -> None:
#     """Persist a batch of Signal records."""
#     # TODO: Add batching and error handling.
#     raise NotImplementedError("Batch signal write not implemented")


# def batch_write_paper_trades(db: Session, trades: Iterable[PaperTrade]) -> None:
#     """Persist a batch of PaperTrade records."""
#     # TODO: Add batching and error handling.
#     raise NotImplementedError("Batch paper trade write not implemented")


# def upsert_wallet_profiles(db: Session, profiles: Iterable[WalletProfile]) -> None:
#     """Upsert wallet profiles in bulk."""
#     # TODO: Implement upsert behavior for wallet profiles.
#     raise NotImplementedError("Wallet profile upsert not implemented")
