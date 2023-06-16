#!/usr/bin/env python3

from testUtils import Utils
from TestHelper import TestHelper
from rodeos_utils import RodeosCluster
import signal
from TestHelper import AppArgs
import time

###############################################################
# rodeos_multi_ship_kill_restart
# 
# 1- Launch a cluster of 2 SHiPs and 2 Rodeos, verifies cluster is operating properly and it is stable.
# 2- Stop a rodeos node and verify the other rodeos is receiving blocks.
# 3- Restart rodeos and verify that rodeos receives blocks and has all the blocks
# 4- Stop a SHiP and verify that the other SHiPs and rodeos nodes are operating properly.
# 5- Restart a SHiP and verify that rodeos listening to its endpoint is again receiving blocks.
#
#This test repeats this scenario for idle state (empty blocks) vs under load test (generating transactions), Unix-socket, TCP/IP, 
# Clean vs non-clean mode restart, and SIGKILL, SIGINT, and SIGTERM kill signals.
#
###############################################################
Print=Utils.Print

extraArgs=AppArgs()
extraArgs.add_bool("--eos-vm-oc-enable", "Use OC for rodeos")
extraArgs.add_bool("--clean-restart", "Use for clean restart of SHiP and Rodeos")
extraArgs.add_bool("--load-test-enable", "Enable load test")
extraArgs.add_bool(flag="--unix-socket", help="Run ship over unix socket")

args=TestHelper.parse_args({"--dump-error-details","--keep-logs","-v","--leave-running","--clean-run"}, extraArgs)
enableOC=args.eos_vm_oc_enable
cleanRestart=args.clean_restart
enableLoadTest=args.load_test_enable
enableUnixSocket=args.unix_socket
Utils.Debug=args.v

TestHelper.printSystemInfo("BEGIN")
testSuccessful=False

def launch_cluster(num_ships, num_rodeos, unix_socket, cleanRestart, killSignal, eos_vm_oc_enable=False):
    with RodeosCluster(args.dump_error_details,
            args.keep_logs,
            args.leave_running,
            args.clean_run, unix_socket,
            'test.filter', './tests/test_filter.wasm',
            eos_vm_oc_enable,
            num_rodeos,
            num_ships) as cluster:

        Print("Testing cluster of {} ship nodes and {} rodeos nodes connecting through {}"\
            .format(num_ships, num_rodeos, (lambda x: 'Unix Socket' if (x==True) else 'TCP')(unix_socket)))
        assert cluster.waitRodeosReady(), "Rodeos failed to stand up for a cluster of {} ship node and {} rodeos node".format(num_ships, num_rodeos)

        if enableLoadTest:
            Print("Starting load generation")
            cluster.startLoad()
        # Big enough to have new blocks produced
        numBlocks=120
        assert cluster.produceBlocks(numBlocks), "Nodeos failed to produce {} blocks for a cluster of {} ship node and {} rodeos node"\
            .format(numBlocks, num_ships, num_rodeos)
        
        for i in range(num_rodeos):
            assert cluster.allBlocksReceived(numBlocks, i), "Rodeos #{} did not receive {} blocks in a cluster of {} ship node and {} rodeos node"\
            .format(i, numBlocks, num_ships, num_rodeos)

        # Stop rodeosId=1
        rodeosKilledId=1
        SHiPIdHostRodeos=cluster.rodeosShipConnectionMap[rodeosKilledId]
        Print("Stopping rodeos #{} with kill -{} signal".format(rodeosKilledId, killSignal))
        cluster.stopRodeos(killSignal, rodeosKilledId)

        # Producing 10 more blocks
        currentBlockNum=cluster.prodNode.getHeadBlockNum()
        numBlocks= currentBlockNum + 10
        assert cluster.produceBlocks(numBlocks), "Nodeos failed to produce {} blocks for a cluster of {} ship node and {} rodeos node"\
            .format(numBlocks, num_ships, num_rodeos)

        # Verify that the other rodeos instances receiving blocks
        for i in range(num_rodeos):
            if i != rodeosKilledId:
                assert cluster.allBlocksReceived(numBlocks, i), "Rodeos #{} did not receive {} blocks after rodeos #{} shutdown"\
                    .format(i, numBlocks, rodeosKilledId)

        # Restarting rodeos        
        Print("Restarting rodeos #{}".format(rodeosKilledId))
        cluster.restartRodeos(SHiPIdHostRodeos, rodeosKilledId, cleanRestart)     
        print("Wait for Rodeos #{} to get ready after a restart".format(rodeosKilledId))
        cluster.waitRodeosReady(rodeosKilledId)
        
        # Producing 10 more blocks after rodeos restart
        currentBlockNum=cluster.prodNode.getHeadBlockNum()
        numBlocks= currentBlockNum + 10
        assert cluster.produceBlocks(numBlocks), "Nodeos failed to produce {} blocks for a cluster of {} ship node and {} rodeos node"\
            .format(numBlocks, num_ships, num_rodeos)

        # verify that rodeos receives all blocks from start to now
        assert cluster.allBlocksReceived(numBlocks, rodeosKilledId), "Rodeos #{} did not receive {} blocks after it restarted"\
                .format(rodeosKilledId, numBlocks)

        # Stop ShipId = 1
        shipKilledId=1
        Print("Stopping SHiP #{} with kill -{} signal".format(shipKilledId, killSignal))
        cluster.stopShip(killSignal, shipKilledId)

        # Producing 10 more blocks after ShiP stop
        currentBlockNum=cluster.prodNode.getHeadBlockNum()
        numBlocks= currentBlockNum + 10
        assert cluster.produceBlocks(numBlocks), "Nodeos failed to produce {} blocks for a cluster of {} ship node and {} rodeos node"\
            .format(numBlocks, num_ships, num_rodeos)

        # verify that other rodeos nodes receiveing blocks without interruption
        for i in cluster.shipNodeIdPortsNodes:
            if i != shipKilledId:
                for j in cluster.ShiprodeosConnectionMap[i]:
                    assert cluster.allBlocksReceived(numBlocks, j), "Rodeos #{} did not receive {} blocks after ship {} shutdown"\
                        .format(j, numBlocks, shipKilledId)

        # Restart Ship
        Print("Restarting SHiP #{}".format(shipKilledId))
        cluster.restartShip(cleanRestart, shipKilledId)
        newShipNode = cluster.cluster.getNode(shipKilledId)
        newShipNode.waitForLibToAdvance()

        # Producing 10 more blocks after ShiP restart
        currentBlockNum=cluster.prodNode.getHeadBlockNum()
        numBlocks= currentBlockNum + 10
        assert cluster.produceBlocks(numBlocks), "Nodeos failed to produce {} blocks for a cluster of {} ship node and {} rodeos node"\
            .format(numBlocks, num_ships, num_rodeos)

        # give it 10 seconds to generate the 10 blocks
        time.sleep(10)

        # Verify that the rodeos node listening to the newly started Ship is receiving blocks.
        for j in cluster.ShiprodeosConnectionMap[shipKilledId]:
            assert cluster.allBlocksReceived(numBlocks, j), "Rodeos #{} did not receive {} blocks after ship #{} restarted"\
                    .format(j, numBlocks, shipKilledId)
        if enableLoadTest:
            cluster.stopLoad()
        cluster.setTestSuccessful(True)


# Test cases: (2 ships, 2 rodeos)
numSHiPs=[2]
numRodeos=[2]
NumTestCase=len(numSHiPs)
for i in range(NumTestCase):
    # SIGTERM is similar to SIGINT. Drop it to reduce testing duration
    for killSignal in [signal.SIGKILL, signal.SIGINT]:
        if killSignal == signal.SIGKILL and cleanRestart == False: # With ungraceful shutdown, clean restart is required.
            continue
        launch_cluster(numSHiPs[i], numRodeos[i], enableUnixSocket, cleanRestart, killSignal, enableOC)


testSuccessful=True
errorCode = 0 if testSuccessful else 1
exit(errorCode)
