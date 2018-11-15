"""
This file is part of nucypher.

nucypher is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
(at your option) any later version.

nucypher is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
GNU General Public License for more details.

You should have received a copy of the GNU General Public License
along with nucypher.  If not, see <https://www.gnu.org/licenses/>.
"""


import os
from ipaddress import ip_address
from urllib.parse import urlparse

import click
import collections
import requests
import shutil
import socket
import time
from eth_utils import is_checksum_address
from nacl.exceptions import CryptoError
from twisted.logger import Logger
from web3.middleware import geth_poa_middleware

from nucypher.blockchain.eth.agents import MinerAgent, PolicyAgent, NucypherTokenAgent
from nucypher.blockchain.eth.chains import Blockchain
from nucypher.characters.lawful import Ursula
from nucypher.cli.constants import BANNER, KEYRING_PASSPHRASE_ENVVAR
from nucypher.config.characters import UrsulaConfiguration
from nucypher.config.constants import USER_LOG_DIR
from nucypher.config.keyring import NucypherKeyring
from nucypher.config.node import NodeConfiguration

UTILITY_LOG = Logger('nucypher-cli.utilities')


#
# Click Eager Functions
#

def echo_version(ctx, param, value):
    if not value or ctx.resilient_parsing:
        return
    click.secho(BANNER, bold=True)
    ctx.exit()


#
# Custom Types
#


PendingConfigurationDetails = collections.namedtuple('PendingConfigurationDetails',
                                                     ('rest_host',    # type: str
                                                      'passphrase',   # type: str
                                                      'wallet',       # type: bool
                                                      'signing',      # type: bool
                                                      'tls',          # type: bool
                                                      'skip_keys',    # type: bool
                                                      'save_file'))   # type: bool


class ChecksumAddress(click.ParamType):
    name = 'checksum_address'

    def convert(self, value, param, ctx):
        if is_checksum_address(value):
            return value
        self.fail('{} is not a valid EIP-55 checksum address'.format(value, param, ctx))


class IPv4Address(click.ParamType):
    name = 'ipv4_address'

    def convert(self, value, param, ctx):
        try:
            _address = ip_address(value)
        except ValueError as e:
            self.fail(str(e))
        else:
            return value


IPV4_ADDRESS = IPv4Address()
CHECKSUM_ADDRESS = ChecksumAddress()


def unlock_and_produce(ursula_config, teacher_nodes=None, **overrides) -> Ursula:
    try:
        # Unlock Keyring - NOTE: Requires ~1GB free memory
        password = os.environ.get(KEYRING_PASSPHRASE_ENVVAR, None)
        if not password:
            password = click.prompt("Password to unlock Ursula's keyring", hide_input=True)

        ursula = ursula_config.produce(passphrase=password,
                                       known_nodes=teacher_nodes,
                                       **overrides)
    except CryptoError:
        click.secho("Invalid keyring passphrase")
        return

    return ursula


def parse_node_uri(uri: str):
    if '@' in uri:
        checksum_address, uri = uri.split("@")
        if not is_checksum_address(checksum_address):
            raise click.BadParameter("{} is not a valid checksum address.".format(checksum_address))
    else:
        checksum_address = None  # federated

    # HTTPS Explicit Required
    parsed_uri = urlparse(uri)
    if not parsed_uri.scheme == "https":
        raise click.BadParameter("Invalid teacher URI. Is the hostname prefixed with 'https://' ?")

    hostname = parsed_uri.hostname
    port = parsed_uri.port or UrsulaConfiguration.DEFAULT_REST_PORT
    return hostname, port, checksum_address


def attempt_seednode_learning(cli_config, teacher_uri, min_stake) -> Ursula:
    hostname, port, checksum_address = parse_node_uri(uri=teacher_uri)
    try:
        teacher = Ursula.from_seed_and_stake_info(host=hostname,
                                                  port=port,
                                                  federated_only=cli_config.federated_only,
                                                  checksum_address=checksum_address,
                                                  minimum_stake=min_stake,
                                                  certificates_directory=cli_config.known_certificates_dir)

    except (socket.gaierror, requests.exceptions.ConnectionError, ConnectionRefusedError):
        UTILITY_LOG.warn("Can't connect to seed node.  Will retry.")
        time.sleep(5)  # TODO: Move this 5

    return teacher


def get_ursula_configuration(dev_mode: bool = False,
                             federated_only: bool = True,
                             checksum_address: str = None,
                             rest_host: str = None,
                             rest_port: str = None,
                             db_name: str = None,
                             config_file: str= None,
                             registry_filepath: str = None,
                             provider_uri: str = None,
                             poa: bool =False,
                             metadata_dir: str = None  # TODO: Scoop up additional Ursulas from metadatas
                             ) -> UrsulaConfiguration:
    """
    Generate an UrsulaConfiguration from disk or for ephemeral development mode.

    All input parameters act as overrides to defaults.

    """

    if dev_mode:

        # Hardcoded Development Configuration
        ursula_config = UrsulaConfiguration(dev=True,
                                            auto_initialize=True,
                                            is_me=True,
                                            rest_host=rest_host,
                                            rest_port=rest_port,
                                            db_name=db_name,
                                            federated_only=federated_only,
                                            registry_filepath=registry_filepath,
                                            provider_uri=provider_uri,
                                            checksum_address=checksum_address,
                                            poa=poa,
                                            save_metadata=False,
                                            load_metadata=False,
                                            start_learning_now=True,
                                            learn_on_same_thread=False,
                                            abort_on_learning_error=True)

    else:
        # Restore Configuration from File with overrides
        try:
            filepath = config_file or UrsulaConfiguration.DEFAULT_CONFIG_FILE_LOCATION
            UTILITY_LOG.debug("Reading Ursula node configuration file {}".format(filepath), fg='blue')
            ursula_config = UrsulaConfiguration.from_configuration_file(filepath=filepath)

        except FileNotFoundError:

            # Hook-up a new partial configuration (Initial Installation)
            ursula_config = UrsulaConfiguration(dev=False,
                                                auto_initialize=False,
                                                is_me=True,
                                                rest_host=rest_host,
                                                rest_port=rest_port,
                                                db_name=db_name,
                                                federated_only=federated_only,
                                                registry_filepath=registry_filepath,
                                                provider_uri=provider_uri,
                                                checksum_address=checksum_address,
                                                poa=poa,
                                                save_metadata=True,
                                                load_metadata=True,
                                                start_learning_now=True,
                                                learn_on_same_thread=False,
                                                abort_on_learning_error=True)

    return ursula_config


def connect_to_blockchain(click_config):
    if click_config.federated_only:
        raise NodeConfiguration.ConfigurationError("Cannot connect to blockchain in federated mode")
    if click_config.deployer:
        click_config.registry_filepath = NodeConfiguration.REGISTRY_SOURCE
    if click_config.compile:
        click.confirm("Compile solidity source?", abort=True)
    click_config.blockchain = Blockchain.connect(provider_uri=click_config.provider_uri,
                                                 deployer=click_config.deployer,
                                                 compile=click_config.compile)
    if click_config.poa:
        w3 = click_config.blockchain.interface.w3
        w3.middleware_stack.inject(geth_poa_middleware, layer=0)
    click_config.accounts = click_config.blockchain.interface.w3.eth.accounts
    click_config.log.debug("CLI established connection to provider {}".format(click_config.blockchain.interface.provider_uri))


def connect_to_contracts(click_config) -> None:
    """Initialize contract agency and set them on config"""
    click_config.token_agent = NucypherTokenAgent(blockchain=click_config.blockchain)
    click_config.miner_agent = MinerAgent(blockchain=click_config.blockchain)
    click_config.policy_agent = PolicyAgent(blockchain=click_config.blockchain)
    click_config.log.debug("CLI established connection to nucypher contracts")


def create_account(click_config, passphrase: str = None) -> str:
    """Creates a new local or hosted ethereum wallet"""
    choice = click.prompt("Create a new Hosted or Local account?", default='hosted',
                          type=click.STRING).strip().lower()
    if choice not in ('hosted', 'local'):
        click.echo("Invalid Input")
        raise click.Abort()

    if not passphrase:
        message = "Enter a passphrase to encrypt your wallet's private key"
        passphrase = click.prompt(message, hide_input=True, confirmation_prompt=True)

    if choice == 'local':
        keyring = NucypherKeyring.generate(passphrase=passphrase,
                                           keyring_root=click_config.node_configuration.keyring_dir,
                                           encrypting=False,
                                           wallet=True)
        new_address = keyring.checksum_address
    elif choice == 'hosted':
        new_address = click_config.blockchain.interface.w3.personal.newAccount(passphrase)
    else:
        raise click.BadParameter("Invalid choice; Options are hosted or local.")
    return new_address


def _collect_pending_configuration_details(ursula: bool = True, rest_host=None) -> PendingConfigurationDetails:

    # Defaults
    generate_wallet = False
    generate_encrypting_keys, generate_tls_keys, save_node_configuration_file = True, True, True

    if ursula and not rest_host:
        rest_host = click.prompt("Enter Node's Public IPv4 Address", type=IPV4_ADDRESS)

    if os.environ.get(KEYRING_PASSPHRASE_ENVVAR):
        passphrase = os.environ.get(KEYRING_PASSPHRASE_ENVVAR)
    else:
        passphrase = click.prompt("Enter a passphrase to encrypt your keyring",
                                  hide_input=True, confirmation_prompt=True)

    details = PendingConfigurationDetails(passphrase=passphrase,
                                          rest_host=rest_host,
                                          wallet=generate_wallet,
                                          signing=generate_encrypting_keys,
                                          tls=generate_tls_keys,
                                          save_file=save_node_configuration_file,
                                          skip_keys=False)
    return details


def write_configuration(ursula_config: UrsulaConfiguration,
                        rest_host: str = None,
                        no_registry: bool = False
                        ) -> None:

    if ursula_config.dev:
        click.secho("Using temporary storage area", fg='blue', bold=True)

    if not no_registry and not ursula_config.federated_only:
        registry_source = ursula_config.REGISTRY_SOURCE
        if not os.path.isfile(registry_source):
            click.echo("Seed contract registry does not exist at path {}.  "
                       "Use --no-registry to skip.".format(registry_source))
            raise click.Abort()

    if ursula_config.config_root:  # Custom installation location
        ursula_config.config_root = ursula_config.config_root
    ursula_config.federated_only = ursula_config.federated_only

    try:
        pending_config = _collect_pending_configuration_details(rest_host=rest_host)
        new_installation_path = ursula_config.initialize(passphrase=pending_config.passphrase,
                                                         wallet=pending_config.wallet,
                                                         encrypting=pending_config.signing,
                                                         tls=pending_config.tls,
                                                         no_registry=no_registry,
                                                         no_keys=pending_config.skip_keys,
                                                         host=pending_config.rest_host)
        if not pending_config.skip_keys:
            click.secho("Generated new keys at {}".format(ursula_config.keyring_dir), fg='green')

    except NodeConfiguration.ConfigurationError as e:
        UTILITY_LOG.critical(str(e))
        raise click.Abort()  # Crash :-(

    else:
        message = "Created nucypher installation files at {}".format(new_installation_path)
        click.secho(message, fg='green')
        UTILITY_LOG.debug(message)

        if pending_config.save_file is True:
            configuration_filepath = ursula_config.to_configuration_file(filepath=ursula_config.config_file_location)
            click.secho("Saved node configuration file {}".format(configuration_filepath), fg='green')
            click.secho("\nTo run an Ursula node from the "
                        "default configuration filepath run 'nucypher ursula run'\n")


def forget_nodes(click_config) -> None:

    def __destroy_dir_contents(path):
        for file in os.listdir(path):
            file_path = os.path.join(path, file)
            if os.path.isfile(file_path):
                os.unlink(file_path)

    click.confirm("Permanently delete all known node data?", abort=True)
    certificates_dir = click_config.node_configuration.known_certificates_dir
    metadata_dir = os.path.join(click_config.node_configuration.known_nodes_dir, 'metadata')

    __destroy_dir_contents(certificates_dir)
    __destroy_dir_contents(metadata_dir)

    message = "Removed all stored node node metadata and certificates"
    click.secho(message=message, fg='red')
    UTILITY_LOG.debug(message)


def destroy_configuration(ursula_config, force: bool = False) -> None:
    if ursula_config.dev:
        raise NodeConfiguration.ConfigurationError("Cannot destroy a temporary node configuration")

    if not force:
        click.confirm('''
    
*Permanently and irreversibly delete all* nucypher files including:
    - Private and Public Keys
    - Known Nodes
    - TLS certificates
    - Node Configurations
    - Log Files
    
Continue?'''.format(ursula_config.config_root), abort=True)

    shutil.rmtree(USER_LOG_DIR)

    try:
        shutil.rmtree(ursula_config.config_root)
    except FileNotFoundError:
        message = 'Failed: No nucypher files found at {}'.format(ursula_config.config_root)
        click.secho(message, fg='red')
        UTILITY_LOG.debug(message)
        raise click.Abort()

    message = "Deleted configuration files at {}".format(ursula_config.config_root)
    click.secho(message, fg='green')
    UTILITY_LOG.debug(message)


def paint_configuration(ursula_config):
    json_config = UrsulaConfiguration._read_configuration_file(filepath=ursula_config.config_file_location)
    click.secho("\n======== Ursula Configuration ======== \n", bold=True)
    for key, value in json_config.items():
        click.secho("{} = {}".format(key, value))
