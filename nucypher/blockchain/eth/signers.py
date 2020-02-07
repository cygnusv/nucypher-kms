from abc import ABC, abstractmethod
from typing import List

from eth_utils import to_checksum_address, to_normalized_address, apply_formatters_to_dict
from hexbytes import HexBytes
from twisted.logger import Logger
from web3 import Web3

from nucypher.blockchain.eth.interfaces import BlockchainInterfaceFactory, BlockchainInterface


class Signer(ABC):

    class AccountLocked(RuntimeError):
        pass

    def __init__(self):
        self.log = Logger(self.__class__.__name__)
        self.__unlocked = False

    @abstractmethod
    def accounts(self) -> List[str]:
        return NotImplemented

    @property
    def is_unlocked(self) -> bool:
        return self.__unlocked

    @abstractmethod
    def unlock_account(self, account: str, password: str, duration: int = None) -> bytes:
        return NotImplemented

    @abstractmethod
    def lock_account(self, account: str) -> str:
        return NotImplemented

    @abstractmethod
    def sign_transaction(self, account: str, transaction: dict) -> HexBytes:
        return NotImplemented

    @abstractmethod
    def sign_message(self, account: str, message: bytes, **kwargs) -> HexBytes:
        return NotImplemented


class Web3Signer(Signer):

    def accounts(self) -> List[str]:
        pass  # TODO

    def __init__(self, client=None):
        super().__init__()
        if not client:
            blockchain = BlockchainInterfaceFactory.get_interface()
            client = blockchain.client
        self.__client = client

    @staticmethod
    def is_device(self, account: str):
        try:
            # TODO: Temporary fix for #1128 and #1385. It's ugly af, but it works. Move somewhere else?
            wallets = self.__client.wallets
        except AttributeError:
            return False
        else:
            HW_WALLET_URL_PREFIXES = ('trezor', 'ledger')
            hw_accounts = [w['accounts'] for w in wallets if w['url'].startswith(HW_WALLET_URL_PREFIXES)]
            hw_addresses = [to_checksum_address(account['address']) for sublist in hw_accounts for account in sublist]
            return account in hw_addresses

    def unlock_account(self, account: str, password: str, duration: int = None):
        if self.is_device:
            unlocked = True
        else:
            unlocked = self.__client.unlock_account(address=account, password=password, duration=duration)
        self.__unlocked = unlocked
        return self.__unlocked

    def lock_account(self, account: str):
        if self.is_device:
            pass  # TODO: Force Disconnect Devices?
        else:
            self.__client.lock_account(address=account)
        self.__unlocked = False
        return self.__unlocked

    def sign_message(self, account: str, message: bytes, **kwargs) -> HexBytes:
        """
        Signs the message with the private key of the TransactingPower.
        """
        if not self.is_unlocked:
            raise self.AccountLocked("Failed to unlock account {}".format(account))
        signature = self.__client.sign_message(account=account, message=message)
        return HexBytes(signature)

    def sign_transaction(self, account: str, unsigned_transaction: dict) -> HexBytes:
        """
        Signs the transaction with the private key of the TransactingPower.
        """
        if not self.is_unlocked:
            raise self.AccountLocked("Failed to unlock account {}".format(account))
        signed_raw_transaction = self.__client.sign_transaction(transaction=unsigned_transaction)
        return signed_raw_transaction


class LocalSigner(Signer):

    def __init__(self, keyfile: str):
        super().__init__()
        self.__key = None
        self.__keyfile = keyfile

    def __import_keyfile(self, password: str) -> bool:
        """
        Import geth formatted key file to the transacting power.
        WARNING: Do not save the key or password anywhere, especially into a shared source file
        """
        w3 = Web3()
        try:
            with open(self.__keyfile) as keyfile:
                encrypted_key = keyfile.read()
                private_key = w3.eth.account.decrypt(encrypted_key, password)
        except FileNotFoundError:
            raise  # TODO
        except Exception:
            raise  # TODO
        else:
            self.__key = private_key
            return True

    def accounts(self) -> List[str]:
        pass  # TODO


    def unlock_account(self, account: str, password: str, duration: int = None) -> bool:
        unlocked = self.__import_keyfile(password=password)
        self.__unlocked = unlocked
        return self.__unlocked

    def lock_account(self, account: str) -> bool:
        self.__key = None
        self.__unlocked = False
        return self.__unlocked

    def sign_transaction(self, account: str, unsigned_transaction: dict) -> HexBytes:
        """
        Signs the transaction with the private key of the TransactingPower.
        """
        if not self.is_unlocked:
            raise self.AccountLocked("Failed to unlock account {}".format(account))
        w3 = Web3()
        signed_transaction = w3.eth.account.sign_transaction(transaction_dict=unsigned_transaction,
                                                             private_key=self.__key)
        signed_raw_transaction = signed_transaction['rawTransaction']
        return signed_raw_transaction

    def sign_message(self, account: str, message: bytes, **kwargs) -> HexBytes:
        pass  # TODO


class ClefSigner(Signer):

    SIGN_DATA_FOR_VALIDATOR = 'data/validator'  # a.k.a. EIP 191 version 0
    SIGN_DATA_FOR_CLIQUE = 'application/clique'  # not relevant for us
    SIGN_DATA_FOR_ECRECOVER = 'text/plain'  # a.k.a. geth's `personal_sign`, EIP-191 version 45 (E)

    DEFAULT_CONTENT_TYPE = SIGN_DATA_FOR_ECRECOVER

    SIGN_DATA_CONTENT_TYPES = (SIGN_DATA_FOR_VALIDATOR, SIGN_DATA_FOR_CLIQUE, SIGN_DATA_FOR_ECRECOVER)

    def __init__(self, w3):
        super().__init__()
        self.w3 = w3

    def is_connected(self) -> bool:
        return True  # TODO: Determine if the socket is reachable

    def accounts(self) -> List[str]:
        normalized_addresses = self.w3.manager.request_blocking("account_list", [])
        checksum_addresses = [to_checksum_address(addr) for addr in normalized_addresses]
        return checksum_addresses

    def sign_transaction(self, account: str, transaction: dict) -> HexBytes:
        formatters = {
            'nonce': Web3.toHex,
            'gasPrice': Web3.toHex,
            'gas': Web3.toHex,
            'value': Web3.toHex,
            'chainId': Web3.toHex,
            'from': to_normalized_address
        }
        transaction = apply_formatters_to_dict(formatters, transaction)
        signed = self.w3.manager.request_blocking("account_signTransaction", [transaction])
        return HexBytes(signed.raw)

    def sign_message(self, account: str, message: bytes, content_type: str = None, validator_address: str = None, **kwargs) -> str:
        # See https://github.com/ethereum/go-ethereum/blob/a32a2b933ad6793a2fe4172cd46c5c5906da259a/signer/core/signed_data.go#L185
        if not content_type:
            content_type = self.DEFAULT_CONTENT_TYPE
        elif content_type not in self.SIGN_DATA_CONTENT_TYPES:
            raise ValueError(f'{content_type} is not a valid content type. '
                             f'Valid types are {self.SIGN_DATA_CONTENT_TYPES}')
        if content_type == self.SIGN_DATA_FOR_VALIDATOR:
            if not validator_address or validator_address == BlockchainInterface.NULL_ADDRESS:
                raise ValueError('When using the intended validator type, a validator address is required.')
            data = [validator_address, message]
        elif content_type == self.SIGN_DATA_FOR_ECRECOVER:
            data = message
        else:
            raise NotImplementedError

        return self.w3.manager.request_blocking("account_signData", [content_type, account, data])

    def unlock_account(self, address: str, password: str, duration: int = None) -> bool:
        return True

    def lock_account(self, address: str):
        return True
