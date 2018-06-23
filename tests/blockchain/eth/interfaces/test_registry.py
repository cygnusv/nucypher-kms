import pytest

from nucypher.blockchain.eth.interfaces import EthereumContractRegistry


def test_contract_registrary(tempfile_path):

    with pytest.raises(EthereumContractRegistry.RegistryError):
        bad_registrar = EthereumContractRegistry(registry_filepath='/fake/file/path/registry.json')
        bad_registrar.search(contract_address='0xdeadbeef')

    # Tests everything is as it should be when initially created
    test_registrar = EthereumContractRegistry(registry_filepath=tempfile_path)

    should_be_empty = test_registrar._EthereumContractRegistry__read()
    assert should_be_empty == []

    # Test contract enrollment and dump_chain
    test_name = 'TestContract'
    test_addr = '0xDEADBEEF'
    test_abi = ['fake', 'data']

    test_registrar.enroll(contract_name=test_name,
                          contract_address=test_addr,
                          contract_abi=test_abi)

    # Search by name...
    contract_records = test_registrar.search(contract_name=test_name)
    assert len(contract_records) == 1, 'More than one record for {}'.format(test_name)
    assert len(contract_records[0]) == 3, 'Registry record is the wrong length'
    name, address, abi = contract_records[0]

    assert name == test_name
    assert address == test_addr
    assert abi == test_abi

    # ...or by address
    contract_record = test_registrar.search(contract_address=test_addr)
    name, address, abi = contract_record

    assert name == test_name
    assert address == test_addr
    assert abi == test_abi

    # Check that searching for an unknown contract raises
    with pytest.raises(EthereumContractRegistry.UnknownContract):
        test_registrar.search(contract_name='this does not exist')

    current_dataset = test_registrar._EthereumContractRegistry__read()
    # Corrupt the registry with a duplicate address
    current_dataset.append([test_name, test_addr, test_abi])
    test_registrar._EthereumContractRegistry__write(current_dataset)

    # Check that searching for an unknown contract raises
    with pytest.raises(EthereumContractRegistry.IllegalRegistrar):
        test_registrar.search(contract_address=test_addr)
