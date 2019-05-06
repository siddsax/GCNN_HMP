import pdb
import copy
import math
import os
import time
import theano
import numpy as np
from theano import tensor as T
from neuralmodels.utils import permute
#from neuralmodels.loadcheckpoint import save, saveSharedRNN, saveSharedRNNVectors, saveSharedRNNOutput, saveMultipleRNNsCombined
from neuralmodels.updates import RMSprop, Adagrad
from neuralmodels.layers.ConcatenateVectors import ConcatenateVectors
from neuralmodels.layers.unConcatenateVectors import *
from neuralmodels.layers.AddNoiseToInput import AddNoiseToInput
from neuralmodels.costs import temp_euc_loss, euclidean_loss, temporal_loss, hinge_euclidean_loss
from neuralmodels.layers.Concatenate_Node_Layers import Concatenate_Node_Layers
from neuralmodels.loadcheckpoint import *
from curriculum import curriculum
import sys
sys.path.append('utils')
from utils import *
from fileIO  import *
from neuralmodels.loadcheckpoint import *
from py_server import ssh
from getError import *

class GCNN(object):
    def __init__(self, args, graphLayers, finalLayer, nodeNames, edgeRNNs, nodeRNNs, nodeToEdgeConnections, edgeListComplete, cost, nodeLabels, learning_rate, new_idx, featureRange, clipnorm=0.0, update_type=RMSprop(), weight_decay=0.0):
        '''
        edgeRNNs and nodeRNNs are dictionary with keys as RNN name and value is a list of layers
        
        nodeToEdgeConnections is a dictionary with keys as nodeRNNs name and value is another dictionary whose keys are edgeRNNs the nodeRNN is connected to and value is a list of size-2 which indicates the features to choose from the unConcatenateLayer 

        nodeLabels is a dictionary with keys as node names and values as Theano matrix
        '''
        self.settings = locals()
        del self.settings['self']
        
        self.edgeRNNs = edgeRNNs
        self.nodeRNNs = nodeRNNs
        self.nodeToEdgeConnections = nodeToEdgeConnections
        self.edgeListComplete = edgeListComplete
        self.nodeLabels = nodeLabels
        self.graphLayers = graphLayers
        self.finalLayer = finalLayer
        self.learning_rate = learning_rate
        self.clipnorm = clipnorm
        self.weight_decay = weight_decay
        
        self.cost = {}
        self.X = {}
        self.Y_pr = {}
        self.Y_pr_last_timestep = {}
        self.Y = {}
        self.args = args
        self.updates = {}
        self.train_node = {}
        self.predict_node = {}
        self.predict_node_last_timestep = {}
        self.grads = {}
        self.predict_node_loss = {}
        self.grad_norm = {}
        self.norm = {}
        self.get_cell = {}

        self.update_type = update_type
        self.update_type.lr = self.learning_rate
        self.update_type.clipnorm = self.clipnorm
        self.std = T.scalar(dtype=theano.config.floatX)

        edgeTypes = edgeRNNs.keys()
        self.num_params = 0

        if(len(self.graphLayers)):
            self.params_all = []
            self.Y_all = T.dtensor3(name="labels")#, dtype=theano.config.floatX)
            self.Y_all.tag.test_value = np.random.rand(7,150, 54)
            # import pdb
            # pdb.set_trace()
            self.masterlayer = unConcatenateVectors(nodeToEdgeConnections)
            self.X_all=self.masterlayer.input#T.tensor3(name="Data", dtype=theano.config.floatX)
        else:
            self.params_all = {}
            self.Y_all = {}
            self.X_all = {}
            self.masterlayer = {}
            for nm in nodeNames:
                self.params_all[nm] = []
                self.Y_all[nm] = T.dtensor3(name="labels")#, dtype=theano.config.floatX)
                self.Y_all[nm].tag.test_value = np.random.rand(7,150, 54)
                self.masterlayer[nm] = unConcatenateVectors(nodeToEdgeConnections[nm],flag=0)
                self.X_all[nm]=self.masterlayer[nm].input#T.tensor3(name="Data", dtype=theano.config.floatX)
        

        for et in edgeTypes:
            layers = self.edgeRNNs[et]
            for i in range(1,len(layers)):
                layers[i].connect(layers[i-1])
                if layers[i].__class__.__name__ == 'AddNoiseToInput':
                    layers[i].std = self.std
            print("======---------=======")
        names = []
        indv_node_layers = []
        for nm in nodeNames:
            edgesConnectedTo = nodeToEdgeConnections[nm].keys()
            layers_below = []
            for et in edgeListComplete:
                if et not in edgesConnectedTo:
                    continue
                edgeLayers = self.edgeRNNs[et]
                
                layers_below.append(edgeLayers)
                if(len(self.graphLayers)):				
                    edgeLayers[0].input = self.masterlayer.output(et,nm)
                else:
                    edgeLayers[0].input = self.masterlayer[nm].output(et)
                    
                
                for l in edgeLayers:
                    if hasattr(l,'params'):
                        if(len(self.graphLayers)):
                            self.params_all.extend(l.params)
                        else:
                            self.params_all[nm].extend(l.params)
                        self.num_params += l.numparams


            cv = ConcatenateVectors()
            cv.connect(layers_below)
            nodeLayers = self.nodeRNNs[nm]
            nodeLayers[0].connect(cv)
            for i in range(1,len(nodeLayers)):
                nodeLayers[i].connect(nodeLayers[i-1])
            print("======---------=======")

            for l in nodeLayers:
                if hasattr(l,'params'):
                    if(len(self.graphLayers)):
                        self.params_all.extend(l.params)
                    else:
                        self.params_all[nm].extend(l.params)
                    self.num_params += l.numparams

            indv_node_layers.append(nodeLayers[-1])
            names.append(nm)
        
    

        if(len(self.graphLayers)):
            cv = Concatenate_Node_Layers()
            cv.connect(indv_node_layers)

# # -------------------------- Graph --------------------------------------
            layers = self.graphLayers
            layers[0].connect(cv)
            for i in range(1,len(layers)):
                layers[i].connect(layers[i-1])
                if layers[i].__class__.__name__ == 'AddNoiseToInput':
                    layers[i].std = self.std

            for l in layers:
                if hasattr(l,'params'):
                    self.params_all.extend(l.params)
                    self.num_params += l.numparams

# -------------------------- --- --------------------------------------
    
        indx = 0
        out = {}

        for nm in nodeNames:
            if(len(self.finalLayer[nm])):
                layers = self.finalLayer[nm]
                if(len(self.graphLayers)):
                    layers[0].connect(self.graphLayers[-1], indx)
                else:
                    layers[0].connect(indv_node_layers[indx])
                for i in range(1,len(layers)):
                    layers[i].connect(layers[i-1])
                    if layers[i].__class__.__name__ == 'AddNoiseToInput':
                        layers[i].std = self.std
                print("======---------=======")
            
                for l in layers:
                    if hasattr(l,'params'):
                        if(len(self.graphLayers)):
                            self.params_all.extend(l.params)
                        else:
                            self.params_all[nm].extend(l.params)
                        self.num_params += l.numparams
                out[nm] = layers[-1].output() 
            else:
                out[nm] =  indv_node_layers[indx].output()
            indx+=1


        if(len(self.graphLayers)):
            self.Y_pr_all = theano_convertToSingleVec(out,new_idx,featureRange)
            
            cost_t = temporal_loss(self.Y_pr_all,5)
            cost_e = euclidean_loss(self.Y_pr_all,self.Y_all)
            self.cost = cost(self.Y_pr_all,self.Y_all)# + normalizing

            print 'Number of parameters in GCNN: ', print_num(self.num_params)
            [self.updates,self.grads] = self.update_type.get_updates(self.params_all,self.cost)			
            print(1, "Defined Updator")
            self.train_node = theano.function([self.X_all,self.Y_all,self.learning_rate,self.std],[self.cost,cost_t,cost_e],updates=self.updates,on_unused_input='ignore')
            print(2, "Defined Trainor")
            self.predict_node = theano.function([self.X_all,self.std],self.Y_pr_all,on_unused_input='ignore')
            print(3, "Defined Predictor")
            self.predict_node_loss = theano.function([self.X_all,self.Y_all,self.std],self.cost,on_unused_input='ignore')
            self.norm = T.sqrt(sum([T.sum(g**2) for g in self.grads]))
            self.grad_norm = theano.function([self.X_all,self.Y_all,self.std],self.norm,on_unused_input='ignore')
            print(6, "Defined GradNorm")
        else:
            
            print 'Number of parameters in GCNN without the graph: ', print_num(self.num_params)
            for nm in nodeNames:
                # k = out[nm].shape
                # out[nm] = out[nm].reshape((k[0],k[1],k[3]))
                self.Y_pr[nm] = out[nm]#nodeLayers[-1].output()
                self.cost[nm] = cost(self.Y_pr[nm],self.Y_all[nm]) + self.weight_decay * nodeLayers[-1].L2_sqr
                [self.updates[nm],self.grads[nm]] = self.update_type.get_updates(self.params_all[nm],self.cost[nm])
                self.train_node[nm] = theano.function([self.X_all[nm],self.Y_all[nm],self.learning_rate,self.std],self.cost[nm],updates=self.updates[nm],on_unused_input='ignore')
                self.predict_node[nm] = theano.function([self.X_all[nm],self.std],self.Y_pr[nm],on_unused_input='ignore')
                self.predict_node_loss[nm] = theano.function([self.X_all[nm],self.Y_all[nm],self.std],self.cost[nm],on_unused_input='ignore')
                self.norm[nm] = T.sqrt(sum([T.sum(g**2) for g in self.grads[nm]]))
                self.grad_norm[nm] = theano.function([self.X_all[nm],self.Y_all[nm],self.std],self.norm[nm],on_unused_input='ignore')
                # self.get_cell[nm] = theano.function([self.X[nm],self.std],nodeLayers[0].layers[0].output(get_cell=True),on_unused_input='ignore')
        

        print("=============")

    def fitModel(self,trX,trY,snapshot_rate=1,path=None,pathD=None,epochs=30,batch_size=50,learning_rate=1e-3,
        learning_rate_decay=0.97,std=1e-5,decay_after=-1,trX_validation=None,trY_validation=None,
        trX_forecasting=None,trY_forecasting=None,trX_forecast_nodeFeatures=None,rng=np.random.RandomState(1234567890),iter_start=None,
        decay_type=None,decay_schedule=None,decay_rate_schedule=None,
        use_noise=False,noise_schedule=None,noise_rate_schedule=None,
        new_idx=None,featureRange=None,poseDataset=None,graph=None,maxiter=10000,ssh_f=0,log=False, num_batches=5):
    
        test_ground_truth = convertToSingleVec(trY_forecasting, new_idx, featureRange)
        test_ground_truth_unnorm = np.zeros((np.shape(test_ground_truth)[0],np.shape(test_ground_truth)[1],len(new_idx)))
        for i in range(np.shape(test_ground_truth)[1]):
            test_ground_truth_unnorm[:,i,:] = unNormalizeData(test_ground_truth[:,i,:],poseDataset.data_mean,poseDataset.data_std,poseDataset.dimensions_to_ignore)

        fname = 'test_ground_truth_unnorm'
        saveForecastedMotion(test_ground_truth_unnorm,path,fname,ssh_flag=int(ssh_f))
        print("---------- Saved Ground Truth -----------------------")

        '''If loading an existing model then some of the parameters needs to be restored'''
        epoch_count = 0
        iterations = 0
        validation_set = []
        skel_loss_after_each_minibatch = []
        loss_after_each_minibatch = []
        complete_logger = ''

        tr_X = {}
        tr_Y = {}
        Nmax = 0
        outputDim = 0
        unequalSize = False
        numExamples = {}
        seq_length = 0
        skel_dim = 0
        

# ----------------------data cleaning related tasks ------------------------------------------------
        nodeNames = trX.keys()

        for nm in nodeNames:
            tr_X[nm] = []
            tr_Y[nm] = []

        for nm in nodeNames:
            N = trX[nm].shape[1]
            seq_length = trX[nm].shape[0]
            skel_dim += trY[nm].shape[2]

            outputDim = trY[nm].ndim
            numExamples[nm] = N
            if Nmax == 0:
                Nmax = N
            if not Nmax == N:
                if N > Nmax:
                    Nmax = N
                unequalSize = True
                
        if trY_forecasting is not None and new_idx is not None:
            trY_forecasting = convertToSingleVec(trY_forecasting,new_idx,featureRange)
            print 'trY_forecasting shape: {0}'.format(trY_forecasting.shape)
            assert(skel_dim == trY_forecasting.shape[2])

        '''Comverting validation set to a single array when doing drop joint experiments'''
        gth = None
        T1 = -1
        N1 = -1	
        if poseDataset.drop_features and unNormalizeData is not None:
            trY_validation = convertToSingleVec(trY_validation,new_idx,featureRange)
            [T1,N1,D1] = trY_validation.shape
            trY_validation_new = np.zeros((T1,N1,poseDataset.data_mean.shape[0]))
            for i in range(N1):
                trY_validation_new[:,i,:] = np.float32(unNormalizeData(trY_validation[:,i,:],poseDataset.data_mean,poseDataset.data_std,poseDataset.dimensions_to_ignore))
            gth = trY_validation_new[poseDataset.drop_start-1:poseDataset.drop_end-1,:,poseDataset.drop_id]

        if unequalSize:
            batch_size = Nmax
        
        batchesX, batchesY = curriculum(num_batches, poseDataset, trX, trY)

        trX = batchesX[0]
        trY = batchesY[0]

        batches_in_one_epoch = 1
        for nm in nodeNames:
            N = trX[nm].shape[1]
            batches_in_one_epoch = int(np.ceil(N*1.0 / batch_size))
            break

        print "batches in each epoch ",batches_in_one_epoch
        #iterations = epoch_count * batches_in_one_epoch * 1.0
        numrange = np.arange(Nmax)
        #for epoch in range(epoch_count,epochs):
        epoch = 0
        from tqdm import tqdm
        curriculum_no = 1
        loss = 10000
        N = trX[nm].shape[1]
        for iterations in range(iter_start, maxiter):
        
            t0 = time.time()
            if(loss < 150):
                trX = batchesX[curriculum_no]
                trY = batchesY[curriculum_no]
                N = trX[nm].shape[1] 
                batches_in_one_epoch = int(np.ceil(N*1.0 / batch_size))
                if(curriculum_no < len(batchesX)-1):
                    curriculum_no+=1
            
            '''Learning rate decay.'''	
            if decay_type:
                if decay_type == 'continuous' and decay_after > 0 and epoch > decay_after:
                    learning_rate *= learning_rate_decay
                elif decay_type == 'schedule' and decay_schedule is not None:
                    for i in range(len(decay_schedule)):
                        if decay_schedule[i] > 0 and iterations > decay_schedule[i]:
                            learning_rate *= decay_rate_schedule[i]
                            decay_schedule[i] = -1

            '''Set noise level.'''	
            if use_noise and noise_schedule is not None:
                for i in range(len(noise_schedule)):
                    if noise_schedule[i] > 0 and iterations >= noise_schedule[i]:
                        std = noise_rate_schedule[i]
                        noise_schedule[i] = -1

            '''Loading noisy data'''
            noisy_data = graph.readCRFgraph(poseDataset,noise=std)
            trX = noisy_data[8]
            trY = noisy_data[9]
            trX_validation = noisy_data[10]
            trY_validation = noisy_data[11]



            '''Permuting before mini-batch iteration'''
            if not unequalSize:
                shuffle_list = rng.permutation(numrange)
                for nm in nodeNames:
                    trX[nm] = trX[nm][:,shuffle_list,:]
                    if outputDim == 2:
                        trY[nm] = trY[nm][:,shuffle_list]
                    elif outputDim == 3:
                        trY[nm] = trY[nm][:,shuffle_list,:]

            for j in range(batches_in_one_epoch):

                examples_taken_from_node = 0	
                for nm in nodeNames:
                    if(len(tr_X[nm])) == 0:
                        examples_taken_from_node = min((j+1)*batch_size,numExamples[nm]) - j*batch_size
                        tr_X[nm] = copy.deepcopy(trX[nm][:,j*batch_size:min((j+1)*batch_size,numExamples[nm]),:])
                        if outputDim == 2:
                            tr_Y[nm] = copy.deepcopy(trY[nm][:,j*batch_size:min((j+1)*batch_size,numExamples[nm])])
                        elif outputDim == 3:
                            tr_Y[nm] = copy.deepcopy(trY[nm][:,j*batch_size:min((j+1)*batch_size,numExamples[nm]),:])
                    else:
                        tr_X[nm] = np.concatenate((tr_X[nm],trX[nm][:,j*batch_size:min((j+1)*batch_size,numExamples[nm]),:]),axis=1)
                        if outputDim == 2:
                            tr_Y[nm] = np.concatenate((tr_Y[nm],trY[nm][:,j*batch_size:min((j+1)*batch_size,numExamples[nm])]),axis=1)
                        elif outputDim == 3:
                            tr_Y[nm] = np.concatenate((tr_Y[nm],trY[nm][:,j*batch_size:min((j+1)*batch_size,numExamples[nm]),:]),axis=1)

                grad_norms = []
# ---------------------------------------------------------------------------------------------
# ------------------------------ Model relted tasks -------------------------------------------
                # for nm in nodeNames:

                if(len(self.graphLayers)):
                    tr_Y_all = convertToSingleVec(tr_Y, new_idx, featureRange)
                    tr_X_all = tr_X[nodeNames[0]]

                    for i in range(1,len(nodeNames)):
                        tr_X_all =  np.concatenate([tr_X_all,tr_X[nodeNames[i]]],axis=2)

                    loss, cost_t, cost_e = self.train_node(tr_X_all,tr_Y_all,learning_rate,std)
                    g = self.grad_norm(tr_X_all,tr_Y_all,std)
                    
                    grad_norms.append(g)
                    
                
                else:
                    loss, skel_loss = 0.0, 0.0
                    g = 0
                
                    for nm in nodeNames:
                        loss_for_current_node = self.train_node[nm](tr_X[nm],tr_Y[nm],learning_rate,std)
                        g = self.grad_norm[nm](tr_X[nm],tr_Y[nm],std)
                        grad_norms.append(g)
                        skel_loss_for_current_node = loss_for_current_node*tr_X[nm].shape[1]*1.0 / examples_taken_from_node
                        loss += loss_for_current_node
                        skel_loss += skel_loss_for_current_node
                    skel_loss_after_each_minibatch.append(skel_loss)
            
                loss_after_each_minibatch.append(loss)
                validation_set.append(-1)
                if(len(self.graphLayers)):
                    termout = 'loss={0} e={1} m={2} g_l2={3} lr={4} noise={5} iter={6} cost_t={7} cost_e={8} num_samples = {9}'.format(
                            loss, epoch, j, grad_norms, learning_rate, std, iterations, cost_t, cost_e, N)
                else:
                    termout = 'e={1} iter={8} m={2} lr={5} g_l2={4} noise={7} loss={0} normalized={3} skel_err={6} num_samples = {9}'.format(
                            loss, epoch, j, (skel_loss*1.0/(seq_length*skel_dim)), grad_norms, learning_rate, np.sqrt(skel_loss*1.0/seq_length), std, iterations, N)

                if log:
                    if (int(ssh_f) == 1):
                        from py_server import ssh
                        ssh("echo " + "'" + termout + "'" + " >> " + path + "/logger.txt")
                    else:
                        thefile = open(path + "/logger.txt", 'ab') 
                        thefile.write(termout)
                        thefile.close()
                #if int(iterations) % (snapshot_rate*4) == 0: 
                out, predicted_test_full, gt_full = getError(self.args, poseDataset, self)
                if int(iterations) % (snapshot_rate*4) == 0:
                   saveForecastedMotion(predicted_test_full, self.args.checkpoint_path, 'test_pred_unnorm')
                   saveForecastedMotion(gt_full, self.args.checkpoint_path, 'test_gt_unnorm')
                
                complete_logger += termout + out +'\n'
                print termout


    # --------------------------- SAVING PERFORMACE CHECKING ET CETRA --------------------------------------------------------


        
                '''Trajectory forecasting on validation set'''
                # if (trX_forecasting is not None) and (trY_forecasting is not None) and path and ((int(iterations) % snapshot_rate == 0)):
                    # if(len(self.graphLayers)):
                    #     forecasted_motion = self.predict_sequence(trX_forecasting,trX_forecast_nodeFeatures,featureRange,new_idx,sequence_length=trY_forecasting.shape[0],poseDataset=poseDataset,graph=graph,Y=trY_forecasting )
                    # else:
                    #     forecasted_motion = self.predict_sequence_indep(trX_forecasting,trX_forecast_nodeFeatures,sequence_length=trY_forecasting.shape[0],poseDataset=poseDataset,graph=graph,Y=trY_forecasting)
                    #     forecasted_motion = convertToSingleVec(forecasted_motion,new_idx,featureRange)

                    # test_forecasted_motion_unnorm = np.zeros(np.shape(test_ground_truth_unnorm))
                    # for i in range(np.shape(test_forecasted_motion_unnorm)[1]):
                    #     test_forecasted_motion_unnorm[:,i,:] = unNormalizeData(forecasted_motion[:,i,:],poseDataset.data_mean,poseDataset.data_std,poseDataset.dimensions_to_ignore)	

                    # print("----------------- Saving Forecast--------------------------")
                    # fname = 'forecast_iteration_unnorm'#_{0}'.format(int(iterations))
                    # saveForecastedMotion(test_forecasted_motion_unnorm,path,fname,ssh_flag=int(ssh_f))
                     
                    # getError(self.args, poseDataset, self)

                del tr_X
                del tr_Y
                            
                tr_X = {}
                tr_Y = {}

                for nm in nodeNames:
                    tr_X[nm] = []
                    tr_Y[nm] = []


                '''Saving the learned model so far'''
                if(len(self.graphLayers)):
                	if int(iterations) % (snapshot_rate*4) == 0:
                 		print ' =======  saving checkpoint.{0} ===== '.format(int(iterations))
                 		saveModel(self, "{0}/checkpoint.{1}".format(path, int(iterations)), "{0}/checkpoint.{1}".format(pathD, int(iterations)))
            

            t1 = time.time()
            termout = 'Epoch took {0} seconds'.format(t1-t0)            
            epoch += 1



# ==============================================================================================
    def predict_sequence_indep(self,teX_original,teX_original_nodeFeatures,sequence_length=100,poseDataset=None,graph=None,Y=None):
        teX = copy.deepcopy(teX_original)
        nodeNames = teX.keys()

        teY = {}
        to_return = {}
        T = 0
        nodeFeatures_t_1 = {}
        for nm in nodeNames:
            [T,N,D] = teX[nm].shape
            to_return[nm] = np.zeros((T+sequence_length,N,D),dtype=theano.config.floatX)
            to_return[nm][:T,:,:] = teX[nm]
            teY[nm] = []
            nodeName = nm.split(':')[0]
            nodeFeatures_t_1[nodeName] = teX_original_nodeFeatures[nm][-1:,:,:]


        for i in range(sequence_length):
            nodeFeatures = {}
            for nm in nodeNames:
                nodeName = nm.split(':')[0]
                prediction = self.predict_node[nm](to_return[nm][:(T+i),:,:],1e-5)
                #nodeFeatures[nodeName] = np.array([prediction])
                nodeFeatures[nodeName] = prediction[-1:,:,:]
                teY[nm].append(nodeFeatures[nodeName][0, :, :])
            for nm in nodeNames:
                nodeName = nm.split(':')[0]
                nodeRNNFeatures = graph.getNodeFeature(nodeName,nodeFeatures,nodeFeatures_t_1,poseDataset)
                a = nodeRNNFeatures[0, :, :]
                to_return[nm][T+i,:,:] = a
            nodeFeatures_t_1 = copy.deepcopy(nodeFeatures)
        for nm in nodeNames:
            teY[nm] = np.array(teY[nm])
        del teX
        return teY

    def predict_sequence(self,teX_original_nodeFeatures,teX_original,featureRange,new_idx,sequence_length=100,poseDataset=None,graph=None,Y=None):
        teX = copy.deepcopy(teX_original)
        nodeNames = teX.keys()

        to_return = {}
        Tc = 0
        body_positions_1 = {}
        teX_original_nodeFeatures_all = teX_original_nodeFeatures[nodeNames[0]]
        for nm in range(1,len(nodeNames)):
            teX_original_nodeFeatures_all =  np.concatenate((teX_original_nodeFeatures_all,teX_original_nodeFeatures[nodeNames[nm]]),axis=2)
        for nm in nodeNames:
            [Tc,N,D] = teX_original[nm].shape ################### ?????????????????
            body_positions_1[nm] = teX_original[nm][-1:,:,:].reshape((1,N,D))

        dim = 0
        for nm in nodeNames:
            idx = new_idx[featureRange[nm]]
            insert_from = np.delete(idx,np.where(idx < 0))
            dim += len(insert_from)

        teY = np.zeros((sequence_length,N,dim))

        for i in range(sequence_length):
            body_positions = {}
            prediction = self.predict_node(teX_original_nodeFeatures_all,1e-5)
            prediction_next = prediction[-1,:,:]
            teY[i,:,:] = prediction_next
            for nm in range(len(nodeNames)):

                idx = new_idx[featureRange[nodeNames[nm]]]
                insert_from = np.delete(idx,np.where(idx < 0))
                a = prediction_next[:,insert_from].reshape((1,N,np.size(prediction_next[:,insert_from])/N))
                body_positions[nodeNames[nm]] = a

            features_all = graph.getNodeFeature(nodeNames[0],body_positions,body_positions_1,poseDataset) 
            for nm in range(1,len(nodeNames)):
                features = graph.getNodeFeature(nodeNames[nm],body_positions,body_positions_1,poseDataset) # previously nodeRNNFeatures, they are concatenation of node and temporal features for the current time step made using nodeFeatures and nodeFeatures-1
                features_all = np.concatenate((features_all,features),axis=2)
            teX_original_nodeFeatures_all = np.concatenate((teX_original_nodeFeatures_all,features_all),axis=0)
        
        del teX
        return teY

#  ============================================================================================================



