    def _no_trade_reasons(self, private_signal: Order, best_public_buy: Order, best_public_sell: Order, margin: int) -> list[str]:
        """
        Get the reasons of no trading
        """
        reasons: list[str] = []
        if private_signal is None:
            reasons.append("no private signal")
            return reasons
        
        if self._role == Role.BUYER and best_public_sell is None:
            reasons.append("no standing sell order in public market")
        if self._role == Role.SELLER and best_public_buy is None:
            reasons.append("no standing buy order in public market")
        
        if margin is None:
            reasons.append("cannot calculate margin due to missing best standing order price")
        else:
            if margin < PROFIT_MARGIN:
                reasons.append("profit margin too low")
        
        if self._holdings is None:
            reasons.append("no holding snapshot yet")
        else:
            if self._role == Role.BUYER:
                if best_public_sell is not None and self._holdings.cash_available < best_public_sell.price:
                    reasons.append("insufficient cash")
            if self._role == Role.SELLER:
                pass
        
        if self._my_standing_order is not None and self._my_standing_order.market.fm_id == PUBLIC_MARKET_ID:
            reasons.append("already have a standing order in public market")

        return reasons