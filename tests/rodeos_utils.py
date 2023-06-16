#!/usr/bin/env python3

from testUtils import Utils
from testUtils import Account
from datetime import datetime
from datetime import timedelta
import time
from Cluster import Cluster
from WalletMgr import WalletMgr
from Node import Node
from Node import BlockType
from TestHelper import TestHelper
from TestHelper import AppArgs

import json
import os
import subprocess
import re
import shutil
import signal
import time
import sys

###############################################################
# rodeos_utils
#
# This file contains common utilities for managing rodeos and
# nodeos with rodeos_plugin.
#
###############################################################

PUBLIC_KEY = "EOS6MRyAjQq8ud7hVNYcfnVPJqcVpscN5So8BhtHuGYqET5GDW5CV"
PRIVATE_KEY = "5KQwrPbwdL6PhXujxW37FSSQZ1JiwsST4cqQzDeyXtP79zkvFD3"

class RodeosCommon:
    @staticmethod
    def createAccount(prodNode, acctName, ownerPubKey=None, activePubkey=None, creatorAcct=Account("eosio")):
        acct = Account(acctName)
        acct.ownerPublicKey = ownerPubKey or PUBLIC_KEY
        acct.activePublicKey = activePubkey or PUBLIC_KEY
        trans = prodNode.createAccount(acct, creatorAcct)
        assert trans, f"Failed to create account {acctName}"
        return acct

    @staticmethod
    def publishContract(prodNode, acct, contractDir, wasmFile=None, abiFile=None, waitForTransBlock=True):
        subdirName = os.path.basename(contractDir)
        wasmFile = wasmFile or subdirName + ".wasm"
        abiFile = abiFile or subdirName + ".abi"
        trans = prodNode.publishContract(acct, contractDir, wasmFile, abiFile, waitForTransBlock=True)
        assert trans, "Failed to publish contract"

    @staticmethod
    def pushTransactionToRodeos(rodeosCluster, rodeosId, trx, acctName):
        Utils.Print(json.dumps(trx))
        for action in trx["actions"]:
            del action["authorization"]
        cmd = ("./programs/cleos/cleos --url {0} push transaction '{1}' --use-old-send-rpc --return-failure-trace 0 --skip-sign --permission {2}"
                .format(rodeosCluster.wqlEndPoints[rodeosId], json.dumps(trx), acctName))
        return subprocess.check_output(cmd, shell=True)

    @staticmethod
    def verifyNodeosRodeosResponses(nodeosResp, rodeosResp):
        fields = [
            "block_num",
            "timestamp",
            "producer",
            "producer_signature",
            "ref_block_prefix",
            "confirmed",
            ("id",),
            ("previous",),
            ("transaction_mroot",),
            ("action_mroot",),
            "schedule_version",
            ]
        for f in fields:
            if isinstance(f, tuple):
                assert nodeosResp[f[0]].upper() == rodeosResp[f[0]].upper(), f[0] + " does not match"
            else:
                assert nodeosResp[f] == rodeosResp[f], f + " does not match"

    @staticmethod
    def getInfoUntilBlock(rodeosCluster, blockNum):
        head_block_num = 0
        while head_block_num < blockNum:
            response = rodeosCluster.getInfo()
            assert "head_block_num" in response, ("Rodeos response does not contain head_block_num, " + "response body = {}".format(json.dumps(response)))
            head_block_num = int(response["head_block_num"])
            if head_block_num < blockNum:
                time.sleep(1)

    @staticmethod
    def verifyProducerNode(prodNode, trx, acctName):
        cmd = ("push transaction '{0}' -p {1}".format(json.dumps(trx), acctName))
        trans = prodNode.processCleosCmd(cmd, cmd, silentErrors=False)
        Utils.Print("trans={}".format(trans))
        assert trans, "Failed to push transaction"

        block_num = int(trans["processed"]["block_num"])
        trx_id = trans["transaction_id"]
        prodNode.waitForIrreversibleBlock(block_num, timeout=60) # Wait until the trx block is executed to become irreversible

        Utils.Print("Verify the account {} from producer node".format(acctName))
        trans = prodNode.getEosAccount(acctName, exitOnError=False)
        assert trans["account_name"], "Failed to get the account {}".format(acctName)

        Utils.Print("Verify the transaction from producer node, " + "block num {}".format(block_num))
        trans_from_full = prodNode.getTransaction(trx_id)
        assert trans_from_full, "Failed to get the transaction with data from the producer node"
        return block_num

    @staticmethod
    def verifyRodeos(rodeosCluster, rodeosId, trx, blockNum, acctName):
        Utils.Print("Verify get info with rodeos")
        RodeosCommon.getInfoUntilBlock(rodeosCluster, blockNum)

        Utils.Print("Verify all blocks received")
        assert rodeosCluster.allBlocksReceived(blockNum), "Rodeos doesn't receive all blocks"

        Utils.Print("Verify get block with rodeos")
        response = rodeosCluster.getBlock(blockNum)
        assert response["block_num"] == blockNum, "Rodeos responds with wrong block"
        nodeos_response = rodeosCluster.prodNode.getBlock(blockNum)
        RodeosCommon.verifyNodeosRodeosResponses(nodeos_response, response)

        Utils.Print("Push transaction directly to rodeos")
        output = RodeosCommon.pushTransactionToRodeos(rodeosCluster, rodeosId, trx, acctName)
        output_dict = json.loads(output)
        assert "processed" in output_dict, "\"processed\" not found, transaction might not be successful"
        Utils.Print("output_dict[\"processed\"]={}".format(output_dict["processed"]))

class RodeosUtils(object):
    def __init__(self, cluster, numRodeos=1, unix_socket_option=False):
        self.cluster=cluster
        self.numRodeos=numRodeos
        self.unix_socket_option=unix_socket_option

        self.producerNodeId=0
        self.rodeosDir=[None] * numRodeos
        self.rodeos=[None] * numRodeos
        self.rodeosStdout=[None] * numRodeos
        self.rodeosStderr=[None] * numRodeos
        self.wqlHostPort=[]
        self.wqlEndPoints=[]

        self.prodNode = self.cluster.getNode(self.producerNodeId)

        port=8880
        for i in range(numRodeos):
            self.rodeosDir[i]=os.path.join(os.getcwd(), 'var/lib/node_0' + str(i+1))
            os.makedirs(self.rodeosDir[i], exist_ok=True)
            self.wqlHostPort.append("127.0.0.1:" + str(port))
            self.wqlEndPoints.append("http://" + self.wqlHostPort[i] + "/")
            port+=1

    def start(self):
        self.prepareLoad()

    def relaunchNode(self, node: Node, chainArg="", relaunchAssertMessage="Fail to relaunch", clean=False):
        if clean:
            shutil.rmtree(Utils.getNodeDataDir(node.nodeId))
            os.makedirs(Utils.getNodeDataDir(node.nodeId))

        # skipGenesis=False starts the same chain

        isRelaunchSuccess=node.relaunch(chainArg=chainArg, timeout=60, skipGenesis=False, cachePopen=True)
        time.sleep(1) # Give a second to replay or resync if needed
        assert isRelaunchSuccess, relaunchAssertMessage
        return isRelaunchSuccess

    def callCmdArrReturnJson(self, rodeosId, endpoint, data=None):
        assert(rodeosId >= 0 and rodeosId < self.numRodeos)

        retry_count = 3
        for _ in range(retry_count):
            try:
                if data is not None:
                    if self.unix_socket_option:
                        return Utils.runCmdArrReturnJson(['curl', '-X', 'POST', '-H', 'Content-Type: application/json', '-H', \
                                                          'Accept: application/json', '--unix-socket', './var/lib/node_0{}/rodeos{}.sock'.format(rodeosId+1, rodeosId) , 'http://localhost/' + endpoint, '--data', json.dumps(data)])
                    return Utils.runCmdArrReturnJson(['curl', '-X', 'POST', '-H', 'Content-Type: application/json', '-H', 'Accept: application/json', self.wqlEndPoints[rodeosId] + endpoint, '--data', json.dumps(data)])
                else:
                    if self.unix_socket_option:
                        return Utils.runCmdArrReturnJson(['curl', '-H', 'Accept: application/json', '--unix-socket', './var/lib/node_0{}/rodeos{}.sock'.format(rodeosId+1, rodeosId), 'http://localhost/' + endpoint])
                    return Utils.runCmdArrReturnJson(['curl', '-X', 'POST', '-H', 'Content-Type: application/json', '-H', 'Accept: application/json', self.wqlEndPoints[rodeosId] + endpoint])
            except subprocess.CalledProcessError as ex:
                # On MacOS, we occassionally get empty return (code 52)
                # Retrying
                if ex.returncode == 52:
                    # On MacOS, we occassionally get empty return. Retrying
                    Utils.Print("runCmdArrReturnJson returned nothing. Retrying...")
                else:
                    return "{ }"

    def getBlock(self, blockNum, rodeosId=0):
        request_body = { "block_num_or_id": blockNum }
        return self.callCmdArrReturnJson(rodeosId, 'v1/chain/get_block', request_body)

    def getInfo(self, rodeosId=0):
        return self.callCmdArrReturnJson(rodeosId, 'v1/chain/get_info')

    def produceBlocks(self, numBlocks):
        Utils.Print("Wait for Nodeos to produce {} blocks".format(numBlocks))
        return self.prodNode.waitForBlock(numBlocks, blockType=BlockType.lib)

    def allBlocksReceived(self, lastBlockNum, rodeosId=0):
        assert(rodeosId >= 0 and rodeosId < self.numRodeos)

        Utils.Print("Verifying {} blocks has received by rodeos #{}".format(lastBlockNum, rodeosId))
        headBlockNum=0
        numSecsSleep=0
        while headBlockNum < lastBlockNum:
            response = self.getInfo(rodeosId)
            jsonResponse = json.dumps(response)
            Utils.Print("response body = {}".format(jsonResponse))
            assert 'head_block_num' in response, f"Rodeos response does not contain head_block_num, response body = {jsonResponse}"
            headBlockNum = int(response['head_block_num'])
            Utils.Print("head_block_num {}".format(headBlockNum))
            if headBlockNum < lastBlockNum:
                if numSecsSleep >= 120: # rodeos might need more time to catch up for load test
                    Utils.Print("Rodeos did not receive block {} after {} seconds. Only block {} received".format(lastBlockNum, numSecsSleep, headBlockNum))
                    return False
                time.sleep(1)
                numSecsSleep+=1
        Utils.Print("{} blocks has received".format(lastBlockNum))

        # find the first block number
        firstBlockNum=0
        for i in range(1, lastBlockNum+1):
            response = self.getBlock(i, rodeosId)
            if "block_num" in response:
                firstBlockNum=response["block_num"]
                Utils.Print("firstBlockNum is {}".format(firstBlockNum))
                break
        assert firstBlockNum >= 1, "firstBlockNum not found"

        Utils.Print("Verifying blocks were received ...")
        for blockNum in range(firstBlockNum, lastBlockNum+1):
            response = self.getBlock(blockNum, rodeosId)
            #Utils.Print("response body = {}".format(json.dumps(response)))
            jsonResponse = json.dumps(response)
            if "block_num" in response:
                assert response["block_num"] == blockNum, f"Rodeos responds with wrong block {i}, response body = {jsonResponse}"
        Utils.Print("All blocks were received in correct order")

        return True
    def create_test_accounts(self): # simulate API /v1/txn_test_gen/create_test_accounts that has a trx ordering problem
        newaccountA = "txn.test.a"
        newaccountB = "txn.test.b"
        newaccountT = "txn.test.t"
        test_accts = [Account(newaccountA), Account(newaccountB), Account(newaccountT)]
        for acct in test_accts:
            acct.ownerPublicKey = acct.activePublicKey  = PUBLIC_KEY
            acct.ownerPrivateKey = acct.activePrivateKey = PRIVATE_KEY
            self.prodNode.createAccount(acct, self.cluster.eosioAccount)

        Utils.Print("set txn.test.t contract to eosio.token & initialize it")
        contractDir="unittests/contracts/eosio.token"
        wasmFile="eosio.token.wasm"
        abiFile="eosio.token.abi"
        Utils.Print("Publish eosio.token contract")
        trans = self.prodNode.publishContract(test_accts[2], contractDir, wasmFile, abiFile, waitForTransBlock=True)

        data = [
            '{"issuer":"' + newaccountT + '", ' + '"maximum_supply":"1000000000.0000 CUR"}',
            '{"to":"' + newaccountT + '", ' + '"quantity":"60000.0000 CUR", "memo":""}',
            '{"from":"' + newaccountT + '", ' + '"to":"' + newaccountA + '", ' + '"quantity":"20000.0000 CUR", "memo":""}',
            '{"from":"' + newaccountT + '", ' + '"to":"' + newaccountB + '", ' + '"quantity":"20000.0000 CUR", "memo":""}'
        ]
        actions = [ "create", "issue", "transfer", "transfer" ]

        for i in range(0, len(actions)):
            (success, trans) = self.prodNode.pushMessage(newaccountT, actions[i], data[i], '-p ' + newaccountT)
            if Utils.Debug:
                Utils.Print("Trans: %s" % trans)
            assert success, "Token initialization should succeed"

    def prepareLoad(self):
        Utils.Print("set contracts and accounts for running load")
        contract="kvload"
        contractDir="unittests/test-contracts/kvload"
        wasmFile="{}.wasm".format(contract)
        abiFile="{}.abi".format(contract)
        try:
            self.prodNode.publishContract(self.cluster.eosioAccount, contractDir, wasmFile, abiFile, True)
        except TypeError:
            # due to empty JSON response, which is expected
            pass

        self.prodNode.pushMessage("eosio", "setkvparam", '[\"ignored\"]', "--permission eosio")

        self.create_test_accounts()

        time.sleep(2)

        testWalletName="rodeotest"
        self.cluster.walletMgr.create(testWalletName)

        # for txn.test.b
        cmd='wallet import -n rodeotest --private-key 5KExyeiK338cxYQo36AmKYrHxRDF9rR4JHFXUR9oZxXbKue7gdL'
        try:
            self.prodNode.processCleosCmd(cmd, cmd, silentErrors=True)
        except TypeError:
            # due to empty JSON response, which is expected
            pass

        cmd='set contract txn.test.b ' + contractDir + ' ' + wasmFile + ' ' + abiFile
        Utils.Print("{}".format(cmd))
        try:
            self.prodNode.processCleosCmd(cmd, cmd, silentErrors=True)
        except TypeError:
            pass

    def startLoad(self, tps=100):
        # period in in milliseconds
        period=20
        # batchSize is number of transactions per period. must even
        batchSize=int(tps//(1000/period))
        if batchSize % 2 != 0:
            batchSize+=1

        cmd="curl -s --data-binary '[\"\", {}, {}]' {}/v1/txn_test_gen/start_generation".format(period, batchSize, self.prodNode.endpointHttp)
        Utils.Print("{}".format(cmd))
        try:
            result = Utils.runCmdReturnJson(cmd)
            Utils.Print("start_generation result {}".format(result))
        except subprocess.CalledProcessError as ex:
            msg=ex.output.decode("utf-8")
            Utils.Print("Exception during start_generation {}".format(msg))
            Utils.errorExit("txn_test_gen/start_generation failed")

    def stopLoad(self):
        cmd="curl -s --data-binary '[\"\"]' %s/v1/txn_test_gen/stop_generation" % (self.prodNode.endpointHttp)
        try:
            result = Utils.runCmdReturnJson(cmd)
            Utils.Print("stop_generation result {}".format(result))
        except subprocess.CalledProcessError as ex:
            msg=ex.output.decode("utf-8")
            Utils.Print("Exception during stop_generation {}".format(msg))
            Utils.errorExit("txn_test_gen/stop_generation failed")


###############################################################
# RodeosCluster
# 
# This class contains common utilities for managing producer,
#   ship, and rodeos. It supports a cluster of one producer,
#   one SHiP, and multiple rodeos'.
#
###############################################################
class RodeosCluster(object):
    def __init__(self, dump_error_details, keep_logs, leave_running, clean_run, unix_socket_option, filterName, filterWasm, enableOC=False, numRodeos=1, numShip=1, timeout=300000, producerExtraArgs=""):
        Utils.Print("Standing up RodeosCluster -- unix_socket_option {}, enableOC {}, numRodeos {}, numShip {}, timeout {}".format(unix_socket_option, enableOC, numRodeos, numShip, timeout))

        self.cluster=Cluster(walletd=True)
        self.dumpErrorDetails=dump_error_details
        self.keepLogs=keep_logs
        self.walletMgr=WalletMgr(True, port=TestHelper.DEFAULT_WALLET_PORT)
        self.testSuccessful=False
        self.killAll=clean_run
        self.killEosInstances=not leave_running
        self.killWallet=not leave_running
        self.clean_run=clean_run

        self.unix_socket_option=unix_socket_option
        self.totalNodes=numShip+1 # Ship nodes + one producer # Number of producer is harded coded 
        self.producerNeverRestarted=True

        self.numRodeos=numRodeos
        self.rodeosDir=[None] * numRodeos
        self.rodeos=[None] * numRodeos
        self.rodeosStdout=[None] * numRodeos
        self.rodeosStderr=[None] * numRodeos
        self.wqlHostPort=[]
        self.wqlEndPoints=[]

        self.numShip=numShip
        self.shipNodeIdPortsNodes={}

        self.rodeosShipConnectionMap={} # stores which rodeos connects to which ship
        self.ShiprodeosConnectionMap={} # stores which ship connects to which rodeos

        port=9999
        for i in range(1, 1+numShip): # One producer
            self.shipNodeIdPortsNodes[i]=["127.0.0.1:" + str(port)]
            port+=1

        port=8880
        for i in range(numRodeos):
            self.rodeosDir[i]=os.path.join(os.getcwd(), 'var/lib/rodeos' + str(i))
            shutil.rmtree(self.rodeosDir[i], ignore_errors=True)
            os.makedirs(self.rodeosDir[i], exist_ok=True)
            self.wqlHostPort.append("127.0.0.1:" + str(port))
            self.wqlEndPoints.append("http://" + self.wqlHostPort[i] + "/")
            port+=1
        

        self.filterName = filterName
        self.filterWasm = filterWasm
        self.OCArg=["--eos-vm-oc-enable"] if enableOC else []
        self.timeout=timeout
        self.producerExtraArgs = producerExtraArgs

    def __enter__(self):
        self.cluster.setWalletMgr(self.walletMgr)
        self.cluster.killall(allInstances=self.clean_run)
        self.cluster.cleanup()
        specificExtraNodeosArgs={}
        # non-producing nodes are at the end of the cluster's nodes, so reserving the last one for SHiP node

        self.producerNodeId=0
        # for load testing
        specificExtraNodeosArgs[self.producerNodeId]="--chain-state-db-size-mb=32768 --plugin eosio::txn_test_gen_plugin --disable-replay-opts {} ".format(self.producerExtraArgs)

        for i in self.shipNodeIdPortsNodes: # Nodeos args for ship nodes.
            specificExtraNodeosArgs[i]=\
                "--plugin eosio::state_history_plugin --trace-history --chain-state-history --chain-state-db-size-mb=32768 --state-history-endpoint {} --disable-replay-opts --plugin eosio::net_api_plugin "\
                    .format(self.shipNodeIdPortsNodes[i][0])
            if self.unix_socket_option:
                specificExtraNodeosArgs[i]+="--state-history-unix-socket-path ship{}.sock".format(i)

        if self.cluster.launch(pnodes=1, totalNodes=self.totalNodes, totalProducers=1, useBiosBootFile=False, specificExtraNodeosArgs=specificExtraNodeosArgs, extraNodeosArgs=" --plugin eosio::trace_api_plugin --trace-no-abis ") is False:
            Utils.cmdError("launcher")
            Utils.errorExit("Failed to stand up eos cluster.")

        for i in self.shipNodeIdPortsNodes:
            self.shipNodeIdPortsNodes[i].append(self.cluster.getNode(i))

        self.prodNode = self.cluster.getNode(self.producerNodeId)

        #verify nodes are in sync and advancing
        self.cluster.waitOnClusterSync(blockAdvancing=5)
        Utils.Print("Cluster in Sync")

        # Shut down bios node such that the cluster contains only one producer,
        # which makes SHiP not fork
        self.cluster.biosNode.kill(signal.SIGTERM)

        self.prepareLoad()

        it=iter(self.shipNodeIdPortsNodes)
        for i in range(self.numRodeos): # connecting each ship to rodeos and if there are more rodeos nodes than ships, rodeos will be connected to same set of ship.
            res = next(it, None)
            if res == None:
                it=iter(self.shipNodeIdPortsNodes)
                res = next(it)
            self.rodeosShipConnectionMap[i]=res
            if res not in self.ShiprodeosConnectionMap:
                self.ShiprodeosConnectionMap[res]=[i]
            else:
                self.ShiprodeosConnectionMap[res].append(i)
            self.restartRodeos(res, i, clean=True)

        self.waitRodeosReady()
        Utils.Print("Rodeos ready")

        return self

    def __exit__(self, exc_type, exc_value, traceback):
        TestHelper.shutdown(self.cluster, self.walletMgr, testSuccessful=self.testSuccessful, killEosInstances=self.killEosInstances, killWallet=self.killWallet, keepLogs=self.keepLogs, cleanRun=self.killAll, dumpErrorDetails=self.dumpErrorDetails)

        for i in range(self.numRodeos):
            if self.rodeos[i] is not None:
                self.rodeos[i].send_signal(signal.SIGTERM)
                self.rodeos[i].wait()
            if self.rodeosStdout[i] is not None:
                self.rodeosStdout[i].close()
            if self.rodeosStderr[i] is not None:
                self.rodeosStderr[i].close()
            if not self.keepLogs and not self.testSuccessful:
                shutil.rmtree(self.rodeosDir[i], ignore_errors=True)

    def relaunchNode(self, node: Node, chainArg="", relaunchAssertMessage="Fail to relaunch", clean=False):
        if clean:
            shutil.rmtree(Utils.getNodeDataDir(node.nodeId))
            os.makedirs(Utils.getNodeDataDir(node.nodeId))

        # skipGenesis=False starts the same chain

        isRelaunchSuccess=node.relaunch(chainArg=chainArg, timeout=30, skipGenesis=False, cachePopen=True)
        time.sleep(1) # Give a second to replay or resync if needed
        assert isRelaunchSuccess, relaunchAssertMessage
        return isRelaunchSuccess

    def restartProducer(self, clean):
        # The first time relaunchNode is called, it does not have
        # "-e -p" for enabling block producing;
        # that's why chainArg="-e -p defproducera " is needed.
        # Calls afterward reuse command in the first call,
        # chainArg is not needed to set any more.
        chainArg=""
        if self.producerNeverRestarted:
            self.producerNeverRestarted=False
            chainArg="-e -p defproducera "

        self.relaunchNode(self.prodNode, chainArg=chainArg, clean=clean)

        if clean:
           self.prepareLoad()

    def stopProducer(self, killSignal):
        self.prodNode.kill(killSignal)


    def restartShip(self, clean, shipNodeId=1):
        assert(shipNodeId in self.shipNodeIdPortsNodes), "ShiP node Id doesn't exist"
        self.relaunchNode(self.shipNodeIdPortsNodes[shipNodeId][1], clean=clean)

    def stopShip(self, killSignal, shipNodeId=1):
        assert(shipNodeId in self.shipNodeIdPortsNodes), "ShiP node Id doesn't exist"
        self.shipNodeIdPortsNodes[shipNodeId][1].kill(killSignal)

    def restartRodeos(self, shipNodeId=1, rodeosId=0, clean=True):
        Utils.Print("restartRodeos -- shipNodeId {}, rodeosId {}, clean {}".format(shipNodeId, rodeosId, clean))
        assert(shipNodeId in self.shipNodeIdPortsNodes), "ShiP node Id doesn't exist"
        assert(rodeosId >= 0 and rodeosId < self.numRodeos)

        if clean:
            if self.rodeosStdout[rodeosId] is not None:
                self.rodeosStdout[rodeosId].close()
            if self.rodeosStderr[rodeosId] is not None:
                self.rodeosStderr[rodeosId].close()
            shutil.rmtree(self.rodeosDir[rodeosId], ignore_errors=True)
            os.makedirs(self.rodeosDir[rodeosId], exist_ok=True)
            self.rodeosStdout[rodeosId]=open(os.path.join(self.rodeosDir[rodeosId], "stdout.out"), "w")
            self.rodeosStderr[rodeosId]=open(os.path.join(self.rodeosDir[rodeosId], "stderr.out"), "w")

        if self.unix_socket_option:
            socket_path=os.path.join(os.getcwd(), Utils.getNodeDataDir(shipNodeId), 'ship{}.sock'.format(shipNodeId))
            Utils.Print("starting rodeos with unix_socket {}".format(socket_path))
            self.rodeos[rodeosId]=subprocess.Popen(['./programs/rodeos/rodeos', '--rdb-database', os.path.join(self.rodeosDir[rodeosId],'rocksdb'),
                                '--data-dir', self.rodeosDir[rodeosId], '--clone-unix-connect-to', socket_path, '--wql-listen', self.wqlHostPort[rodeosId],
                                '--wql-unix-listen', './var/lib/rodeos{}/rodeos{}.sock'.format(rodeosId, rodeosId),'--wql-threads', '8', '--wql-idle-timeout', str(self.timeout),
                                '--filter-name', self.filterName , '--filter-wasm', self.filterWasm ] + self.OCArg,
                                stdout=self.rodeosStdout[rodeosId], stderr=self.rodeosStderr[rodeosId])
        else: # else means TCP/IP
            Utils.Print("starting rodeos with TCP {}".format(self.shipNodeIdPortsNodes[shipNodeId][0]))
            self.rodeos[rodeosId]=subprocess.Popen(['./programs/rodeos/rodeos', '--rdb-database', os.path.join(self.rodeosDir[rodeosId],'rocksdb'),
                                '--data-dir', self.rodeosDir[rodeosId], '--clone-connect-to', self.shipNodeIdPortsNodes[shipNodeId][0], '--wql-listen'
                                , self.wqlHostPort[rodeosId], '--wql-threads', '8', '--wql-idle-timeout', str(self.timeout), '--filter-name', self.filterName , '--filter-wasm', self.filterWasm ] + self.OCArg,
                                stdout=self.rodeosStdout[rodeosId], stderr=self.rodeosStderr[rodeosId])

    # SIGINT to simulate CTRL-C
    def stopRodeos(self, killSignal=signal.SIGINT, rodeosId=0):
        assert(rodeosId >= 0 and rodeosId < self.numRodeos)
        if self.rodeos[rodeosId] is not None:
            self.rodeos[rodeosId].send_signal(killSignal)
            self.rodeos[rodeosId].wait()
            self.rodeos[rodeosId] = None

    def waitRodeosReady(self, rodeosId=0):
        assert(rodeosId >= 0 and rodeosId < self.numRodeos)
        if self.unix_socket_option:
            return Utils.waitForTruth(lambda:  Utils.runCmdArrReturnStr(['curl', '-H', 'Accept: application/json', '--unix-socket', './var/lib/rodeos{}/rodeos{}.sock'.format(rodeosId, rodeosId), 'http://localhost/v1/chain/get_info'], silentErrors=True) != "" , timeout=60)
        return Utils.waitForTruth(lambda:  Utils.runCmdArrReturnStr(['curl', '-H', 'Accept: application/json', self.wqlEndPoints[rodeosId] + 'v1/chain/get_info'], silentErrors=True) != "" , timeout=60)

    def callCmdArrReturnJson(self, rodeosId, endpoint, data=None):
        assert(rodeosId >= 0 and rodeosId < self.numRodeos)

        retry_count = 3
        for _ in range (retry_count):
            try:
                if data is not None:
                    if self.unix_socket_option:
                        return Utils.runCmdArrReturnJson(['curl', '-X', 'POST', '-H', 'Content-Type: application/json', '-H', \
                            'Accept: application/json', '--unix-socket', './var/lib/rodeos{}/rodeos{}.sock'.format(rodeosId, rodeosId) , 'http://localhost/' + endpoint, '--data', json.dumps(data)])
                    return Utils.runCmdArrReturnJson(['curl', '-X', 'POST', '-H', 'Content-Type: application/json', '-H', 'Accept: application/json', self.wqlEndPoints[rodeosId] + endpoint, '--data', json.dumps(data)])
                else:
                    if self.unix_socket_option:
                        return Utils.runCmdArrReturnJson(['curl', '-H', 'Accept: application/json', '--unix-socket', './var/lib/rodeos{}/rodeos{}.sock'.format(rodeosId, rodeosId), 'http://localhost/' + endpoint])
                    return Utils.runCmdArrReturnJson(['curl', '-X', 'POST', '-H', 'Content-Type: application/json', '-H', 'Accept: application/json', self.wqlEndPoints[rodeosId] + endpoint])
            except subprocess.CalledProcessError as ex:
                # On MacOS, we occassionally get empty return (code 52)
                # Retrying
                if ex.returncode == 52:
                    # On MacOS, we occassionally get empty return. Retrying
                    Utils.Print("runCmdArrReturnJson returned nothing. Retrying...")
                else:
                    return "{ }"

    def getBlock(self, blockNum, rodeosId=0):
        request_body = { "block_num_or_id": blockNum }
        return self.callCmdArrReturnJson(rodeosId, 'v1/chain/get_block', request_body)

    def getInfo(self, rodeosId=0):
        return self.callCmdArrReturnJson(rodeosId, 'v1/chain/get_info')

    def produceBlocks(self, numBlocks):
        Utils.Print("Wait for Nodeos to produce {} blocks".format(numBlocks))
        return self.prodNode.waitForBlock(numBlocks, blockType=BlockType.lib)

    def allBlocksReceived(self, lastBlockNum, rodeosId=0):
        assert(rodeosId >= 0 and rodeosId < self.numRodeos)

        Utils.Print("Verifying {} blocks has received by rodeos #{}".format(lastBlockNum, rodeosId))
        headBlockNum=0
        numSecsSleep=0
        while headBlockNum < lastBlockNum:
            response = self.getInfo(rodeosId)
            assert 'head_block_num' in response, "Rodeos response does not contain head_block_num, response body = {}".format(json.dumps(response))
            headBlockNum = int(response['head_block_num'])
            Utils.Print("head_block_num {}".format(headBlockNum))
            if headBlockNum < lastBlockNum:
                if numSecsSleep >= 120: # rodeos might need more time to catch up for load test
                    Utils.Print("Rodeos did not receive block {} after {} seconds. Only block {} received".format(lastBlockNum, numSecsSleep, headBlockNum))
                    return False
                time.sleep(1)
                numSecsSleep+=1
        Utils.Print("{} blocks has received".format(lastBlockNum))
        
        # find the first block number
        firstBlockNum=0
        for i in range(1, lastBlockNum+1):
            response = self.getBlock(i, rodeosId)
            if "block_num" in response:
                firstBlockNum=response["block_num"]
                Utils.Print("firstBlockNum is {}".format(firstBlockNum))
                break
        assert firstBlockNum >= 1, "firstBlockNum not found"

        Utils.Print("Verifying blocks were received ...")
        for blockNum in range(firstBlockNum, lastBlockNum+1):
            response = self.getBlock(blockNum, rodeosId)
            #Utils.Print("response body = {}".format(json.dumps(response)))
            if "block_num" in response:
                assert response["block_num"] == blockNum, "Rodeos responds with wrong block {0}, response body = {1}".format(i, json.dumps(response))
        Utils.Print("All blocks were received in correct order")

        return True

    def setTestSuccessful(self, testSuccessful):
        self.testSuccessful=testSuccessful

    def create_test_accounts(self): # simulate API /v1/txn_test_gen/create_test_accounts that has a trx ordering problem
        newaccountA = "txn.test.a"
        newaccountB = "txn.test.b"
        newaccountT = "txn.test.t"
        test_accts = [Account(newaccountA), Account(newaccountB), Account(newaccountT)]
        for acct in test_accts:
            acct.ownerPublicKey = acct.activePublicKey  = PUBLIC_KEY
            acct.ownerPrivateKey = acct.activePrivateKey = PRIVATE_KEY
            self.prodNode.createAccount(acct, self.cluster.eosioAccount)

        Utils.Print("set txn.test.t contract to eosio.token & initialize it")
        contractDir="unittests/contracts/eosio.token"
        wasmFile="eosio.token.wasm"
        abiFile="eosio.token.abi"
        Utils.Print("Publish eosio.token contract")
        trans = self.prodNode.publishContract(test_accts[2], contractDir, wasmFile, abiFile, waitForTransBlock=True)

        data = [
            '{"issuer":"' + newaccountT + '", ' + '"maximum_supply":"1000000000.0000 CUR"}',
            '{"to":"' + newaccountT + '", ' + '"quantity":"60000.0000 CUR", "memo":""}',
            '{"from":"' + newaccountT + '", ' + '"to":"' + newaccountA + '", ' + '"quantity":"20000.0000 CUR", "memo":""}',
            '{"from":"' + newaccountT + '", ' + '"to":"' + newaccountB + '", ' + '"quantity":"20000.0000 CUR", "memo":""}'
        ]
        actions = [ "create", "issue", "transfer", "transfer" ]

        for i in range(0, len(actions)):
            (success, trans) = self.prodNode.pushMessage(newaccountT, actions[i], data[i], '-p ' + newaccountT)
            if Utils.Debug:
                Utils.Print("Trans: %s" % trans)
            assert success, "Token initialization should succeed"

    def prepareLoad(self):
        Utils.Print("set contracts and accounts for running load")
        contract="kvload"
        contractDir="unittests/test-contracts/kvload"
        wasmFile="{}.wasm".format(contract)
        abiFile="{}.abi".format(contract)
        try:
            self.prodNode.publishContract(self.cluster.eosioAccount, contractDir, wasmFile, abiFile, True)
        except TypeError:
            # due to empty JSON response, which is expected
            pass

        self.prodNode.pushMessage("eosio", "setkvparam", '[\"ignored\"]', "--permission eosio")

        self.create_test_accounts()

        time.sleep(2)

        testWalletName="rodeotest"
        self.walletMgr.create(testWalletName)

        # for txn.test.b
        cmd='wallet import -n rodeotest --private-key 5KExyeiK338cxYQo36AmKYrHxRDF9rR4JHFXUR9oZxXbKue7gdL'
        try:
            self.prodNode.processCleosCmd(cmd, cmd, silentErrors=True)
        except TypeError:
            # due to empty JSON response, which is expected
            pass

        cmd='set contract txn.test.b ' + contractDir + ' ' + wasmFile + ' ' + abiFile
        Utils.Print("{}".format(cmd))
        try:
            self.prodNode.processCleosCmd(cmd, cmd, silentErrors=True)
        except TypeError:
            pass

    def startLoad(self, tps=100):
        # period in in milliseconds
        period=20
        # batchSize is number of transactions per period. must even
        batchSize=int(tps//(1000/period))
        if batchSize % 2 != 0:
            batchSize+=1

        cmd="curl -s --data-binary '[\"\", {}, {}]' {}/v1/txn_test_gen/start_generation".format(period, batchSize, self.prodNode.endpointHttp)
        Utils.Print("{}".format(cmd))
        try:
            result = Utils.runCmdReturnJson(cmd)
            Utils.Print("start_generation result {}".format(result))
        except subprocess.CalledProcessError as ex:
            msg=ex.output.decode("utf-8")
            Utils.Print("Exception during start_generation {}".format(msg))
            Utils.errorExit("txn_test_gen/start_generation failed")

    def stopLoad(self):
        cmd="curl -s --data-binary '[\"\"]' %s/v1/txn_test_gen/stop_generation" % (self.prodNode.endpointHttp)
        try:
            result = Utils.runCmdReturnJson(cmd)
            Utils.Print("stop_generation result {}".format(result))
        except subprocess.CalledProcessError as ex:
            msg=ex.output.decode("utf-8")
            Utils.Print("Exception during stop_generation {}".format(msg))
            Utils.errorExit("txn_test_gen/stop_generation failed")
