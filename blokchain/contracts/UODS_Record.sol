// SPDX-License-Identifier: MIT
pragma solidity ^0.8.0;

contract UODS_Record {
    
    struct Record {
        string ipfsCID;
        uint256 timestamp;
        address verifier;
    }
    
    mapping (bytes32 => Record) private records;
    
    event RecordCreated(bytes32 indexed key, string ipfsCID, uint256 timestamp);

    // TODO: addRecord ve getRecord fonksiyonları eklenecek
}