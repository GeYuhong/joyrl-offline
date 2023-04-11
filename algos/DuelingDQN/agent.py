#!/usr/bin/env python
# coding=utf-8
'''
Author: JiangJi
Email: johnjim0816@gmail.com
Date: 2022-11-14 23:50:59
LastEditor: JiangJi
LastEditTime: 2023-03-24 22:59:13
Discription: 
'''
import torch
import torch.nn as nn
import torch.optim as optim
import math,random
import ray
import numpy as np
from common.memories import ReplayBuffer
from common.optms import SharedAdam

class DuelingQNetwork(nn.Module):
    def __init__(self, n_states, n_actions,hidden_dim=128):
        super(DuelingQNetwork, self).__init__()
        # 隐藏层
        self.hidden_layer = nn.Sequential(
            nn.Linear(n_states, hidden_dim),
            nn.ReLU()
        )
        #  优势层
        self.advantage_layer = nn.Sequential(
            nn.Linear(hidden_dim, hidden_dim),
            nn.ReLU(),
            nn.Linear(hidden_dim, n_actions)
        )
        # 价值层
        self.value_layer = nn.Sequential(
            nn.Linear(hidden_dim, hidden_dim),
            nn.ReLU(),
            nn.Linear(hidden_dim, 1)
        )
        
    def forward(self, state):
        x = self.hidden_layer(state)
        advantage = self.advantage_layer(x)
        value     = self.value_layer(x)
        return value + advantage - advantage.mean() # Q(s,a) = V(s) + A(s,a) - mean(A(s,a))
        
class Agent:
    def __init__(self, cfg, is_share_agent = False) -> None:
        '''智能体类
        Args:
            cfg (class): 超参数类
            is_share_agent (bool, optional): 是否为共享的 Agent ，多进程下使用，默认为 False
        '''
        self.n_actions = cfg.n_actions
        self.device = torch.device(cfg.device) 
        self.gamma = cfg.gamma  
        ## e-greedy parameters
        self.sample_count = 0  # sample count for epsilon decay
        self.epsilon_start = cfg.epsilon_start
        self.epsilon_end = cfg.epsilon_end
        self.epsilon_decay = cfg.epsilon_decay
        self.batch_size = cfg.batch_size
        self.target_update = cfg.target_update
        self.policy_net = DuelingQNetwork(cfg.n_states,cfg.n_actions,hidden_dim=cfg.hidden_dim).to(self.device)
        self.target_net = DuelingQNetwork(cfg.n_states,cfg.n_actions,hidden_dim=cfg.hidden_dim).to(self.device)
        ## copy parameters from policy net to target net
        for target_param, param in zip(self.target_net.parameters(),self.policy_net.parameters()): 
            target_param.data.copy_(param.data)
        # self.target_net.load_state_dict(self.policy_net.state_dict()) # or use this to copy parameters
        self.optimizer = optim.Adam(self.policy_net.parameters(), lr=cfg.lr) 
        self.memory = ReplayBuffer(cfg.buffer_size)
        self.update_flag = False 
        if is_share_agent:
            self.policy_net.share_memory()
            self.optimizer = SharedAdam(self.policy_net.parameters(), lr=cfg.lr)
            self.optimizer.share_memory()
        
    def sample_action(self,state):
        ''' 采样动作
        Args:
            state (array): 状态
        Returns:
            action (int): 动作
        '''
        self.sample_count += 1
        # epsilon must decay(linear,exponential and etc.) for balancing exploration and exploitation
        self.epsilon = self.epsilon_end + (self.epsilon_start - self.epsilon_end) * \
            math.exp(-1. * self.sample_count / self.epsilon_decay) 
        if random.random() > self.epsilon:
            with torch.no_grad():
                state = torch.tensor(state, device=self.device, dtype=torch.float32).unsqueeze(dim=0)
                q_values = self.policy_net(state)
                action = q_values.max(1)[1].item() # choose action corresponding to the maximum q value
        else:
            action = random.randrange(self.n_actions)
        return action
    @torch.no_grad()
    def predict_action(self,state):
        ''' 预测动作
        Args:
            state (array): 状态
        Returns:
            action (int): 动作
        '''
        state = torch.tensor(state, device=self.device, dtype=torch.float32).unsqueeze(dim=0)
        q_values = self.policy_net(state)
        action = q_values.max(1)[1].item() # choose action corresponding to the maximum q value
        return action
    def update(self, share_agent=None):
        if len(self.memory) < self.batch_size: # when transitions in memory donot meet a batch, not update
            return
        # else:
        #     if not self.update_flag:
        #         print("Begin to update!")
        #         self.update_flag = True
        # sample a batch of transitions from replay buffer
        state_batch, action_batch, reward_batch, next_state_batch, done_batch = self.memory.sample(
            self.batch_size)
        state_batch = torch.tensor(np.array(state_batch), device=self.device, dtype=torch.float) # shape(batchsize,n_states)
        action_batch = torch.tensor(action_batch, device=self.device).unsqueeze(1) # shape(batchsize,1)
        reward_batch = torch.tensor(reward_batch, device=self.device, dtype=torch.float).unsqueeze(1) # shape(batchsize,1)
        next_state_batch = torch.tensor(np.array(next_state_batch), device=self.device, dtype=torch.float) # shape(batchsize,n_states)
        done_batch = torch.tensor(np.float32(done_batch), device=self.device).unsqueeze(1) # shape(batchsize,1)
        # print(state_batch.shape,action_batch.shape,reward_batch.shape,next_state_batch.shape,done_batch.shape)
        # compute current Q(s_t,a), it is 'y_j' in pseucodes
        q_value_batch = self.policy_net(state_batch).gather(dim=1, index=action_batch) # shape(batchsize,1),requires_grad=True
        # print(q_values.requires_grad)
        # compute max(Q(s_t+1,A_t+1)) respects to actions A, next_max_q_value comes from another net and is just regarded as constant for q update formula below, thus should detach to requires_grad=False
        next_max_q_value_batch = self.target_net(next_state_batch).max(1)[0].detach().unsqueeze(1) 
        # print(q_values.shape,next_q_values.shape)
        # compute expected q value, for terminal state, done_batch[0]=1, and expected_q_value=rewardcorrespondingly
        expected_q_value_batch = reward_batch + self.gamma * next_max_q_value_batch* (1-done_batch)
        # print(expected_q_value_batch.shape,expected_q_value_batch.requires_grad)
        loss = nn.MSELoss()(q_value_batch, expected_q_value_batch)  # shape same to 
        if share_agent is not None:
            # Clear the gradient of the previous step of share_agent
            share_agent.optimizer.zero_grad()
            loss.backward()
            # clip to avoid gradient explosion
            for param in self.policy_net.parameters():  
                param.grad.data.clamp_(-1, 1)
            # Copy the gradient from policy_net of local_agnet to policy_net of share_agent
            for param, share_param in zip(self.policy_net.parameters(), share_agent.policy_net.parameters()):
                share_param._grad = param.grad
            share_agent.optimizer.step()
            self.policy_net.load_state_dict(share_agent.policy_net.state_dict())
            if self.sample_count % self.target_update == 0: # target net update, target_update means "C" in pseucodes
                self.target_net.load_state_dict(self.policy_net.state_dict()) 
        # backpropagation
        else:
            self.optimizer.zero_grad()  
            loss.backward()
            # clip to avoid gradient explosion
            for param in self.policy_net.parameters():  
                param.grad.data.clamp_(-1, 1)
            self.optimizer.step() 
            if self.sample_count % self.target_update == 0: # target net update, target_update means "C" in pseucodes
                self.target_net.load_state_dict(self.policy_net.state_dict())   

    def update_ray(self, share_agent_policy_net, share_agent_optimizer):
        """Update the share_agent parameters with ray"""
        if len(self.memory) < self.batch_size: # when transitions in memory donot meet a batch, not update
            return share_agent_policy_net, share_agent_optimizer
        state_batch, action_batch, reward_batch, next_state_batch, done_batch = self.memory.sample(
            self.batch_size)
        state_batch = torch.tensor(np.array(state_batch), device=self.device, dtype=torch.float) # shape(batchsize,n_states)
        action_batch = torch.tensor(action_batch, device=self.device).unsqueeze(1) # shape(batchsize,1)
        reward_batch = torch.tensor(reward_batch, device=self.device, dtype=torch.float).unsqueeze(1) # shape(batchsize,1)
        next_state_batch = torch.tensor(np.array(next_state_batch), device=self.device, dtype=torch.float) # shape(batchsize,n_states)
        done_batch = torch.tensor(np.float32(done_batch), device=self.device).unsqueeze(1) # shape(batchsize,1)
        # print(state_batch.shape,action_batch.shape,reward_batch.shape,next_state_batch.shape,done_batch.shape)
        # compute current Q(s_t,a), it is 'y_j' in pseucodes
        q_value_batch = self.policy_net(state_batch).gather(dim=1, index=action_batch) # shape(batchsize,1),requires_grad=True
        # print(q_values.requires_grad)
        # compute max(Q(s_t+1,A_t+1)) respects to actions A, next_max_q_value comes from another net and is just regarded as constant for q update formula below, thus should detach to requires_grad=False
        next_max_q_value_batch = self.target_net(next_state_batch).max(1)[0].detach().unsqueeze(1) 
        # print(q_values.shape,next_q_values.shape)
        # compute expected q value, for terminal state, done_batch[0]=1, and expected_q_value=rewardcorrespondingly
        expected_q_value_batch = reward_batch + self.gamma * next_max_q_value_batch* (1-done_batch)
        # print(expected_q_value_batch.shape,expected_q_value_batch.requires_grad)
        loss = nn.MSELoss()(q_value_batch, expected_q_value_batch)
        share_agent_optimizer.zero_grad()
        loss.backward()
        # clip to avoid gradient explosion
        for param in self.policy_net.parameters():  
            param.grad.data.clamp_(-1, 1)
        for param, share_param in zip(self.policy_net.parameters(), share_agent_policy_net.parameters()):
            share_param._grad = param.grad
        share_agent_optimizer.step()
        self.policy_net.load_state_dict(share_agent_policy_net.state_dict())
        if self.sample_count % self.target_update == 0: # target net update, target_update means "C" in pseucodes
            self.target_net.load_state_dict(self.policy_net.state_dict())
        return share_agent_policy_net, share_agent_optimizer
    
    def save_model(self, fpath):
        from pathlib import Path
        # create path
        Path(fpath).mkdir(parents=True, exist_ok=True)
        torch.save(self.policy_net.state_dict(), f"{fpath}/checkpoint.pt")

    def load_model(self, fpath):
        checkpoint = torch.load(f"{fpath}/checkpoint.pt", map_location=self.device)
        self.target_net.load_state_dict(checkpoint)
        for target_param, param in zip(self.target_net.parameters(), self.policy_net.parameters()):
            param.data.copy_(target_param.data)

@ray.remote
class ShareAgent:
    def __init__(self, cfg):
        '''共享智能体类
        Args:
            cfg (class): 超参数类
        '''
        self.policy_net = DuelingQNetwork(cfg.n_states,cfg.n_actions,hidden_dim=cfg.hidden_dim).to(cfg.device)
        self.target_net = DuelingQNetwork(cfg.n_states,cfg.n_actions,hidden_dim=cfg.hidden_dim).to(cfg.device)
        self.optimizer = SharedAdam(self.policy_net.parameters(), lr=cfg.lr) 
        self.lr = cfg.lr
        # self.memory = ReplayBuffer(cfg.buffer_size)

    def get_parameters(self):
        return self.policy_net, self.optimizer
    
    def receive_parameters(self, policy_net, optimizer):
        self.policy_net.load_state_dict(policy_net.state_dict())
        # self.optimizer = optim.Adam(self.policy_net.parameters(), lr=self.lr) 

    def save_model(self, fpath):
        from pathlib import Path
        # create path
        Path(fpath).mkdir(parents=True, exist_ok=True)
        torch.save(self.policy_net.state_dict(), f"{fpath}/checkpoint.pt")

    def load_model(self, fpath):
        self.policy_net.load_state_dict(torch.load(f"{fpath}/checkpoint.pt"))
        for target_param, param in zip(self.target_net.parameters(), self.policy_net.parameters()):
            target_param.data.copy_(param.data)

    
    def update_parameters(self, local_net):
        """training algorithm in ShareAgent"""
        self.optimizer.zero_grad()
        for param, share_param in zip(local_net.parameters(), self.policy_net.parameters()):
            share_param._grad = param.grad
        self.optimizer.step()
        return self.policy_net
    
    def get_share_net(self):
        return self.policy_net